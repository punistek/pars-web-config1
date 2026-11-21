"""Offline tests. Run: python -m unittest discover -s tests -v"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vavoo_bot as bot


EXAMPLE_M3U = '''#EXTM3U
#EXTINF:-1 tvg-id="TRT1.tr" tvg-name="TRT 1 HD" tvg-logo="https://images.example/trt.png" group-title="Turkey" http-user-agent="VAVOO/1.0" http-referrer="https://vavoo.to/",TRT 1 HD
https://stream.example/live/trt.m3u8
#EXTINF:-1 group-title="Turkey" tvg-name="TRT 1 HD",TRT 1 HD
https://stream.example/live/trt-other.m3u8
#EXTINF:-1 group-title="Germany",Sample TV
https://stream.example/stream.ts
#EXTINF:-1 tvg-name="Bad TV" group-title="Germany",Bad TV
https://stream.example/broken.m3u8
'''


class TestParse(unittest.TestCase):
    def test_m3u_parses_headers_and_groups(self):
        entries = bot.parse_m3u(EXAMPLE_M3U, 'https://www2.vavoo.to/live2/index')
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0].name, 'TRT 1 HD')
        self.assertEqual(entries[0].group, 'Turkey')
        self.assertEqual(entries[0].tvg_id, 'TRT1.tr')
        self.assertEqual(entries[0].headers['User-Agent'], 'VAVOO/1.0')
        self.assertEqual(entries[0].headers['Referer'], 'https://vavoo.to/')
        self.assertEqual(entries[2].country, '')
        self.assertEqual(bot.infer_country(entries[2]), 'DE')

    def test_extgrp_and_opts_and_url_suffix(self):
        playlist = '''#EXTM3U\n#EXTINF:-1,Channel\n#EXTGRP:TR | Haber\n#EXTVLCOPT:http-user-agent=ExampleAgent\nhttps://a.example/live.m3u8|Referer=https%3A%2F%2Fexample.com%2F\n'''
        ch = bot.parse_m3u(playlist, 'https://a.example')[0]
        self.assertEqual(ch.group, 'TR | Haber')
        self.assertEqual(ch.headers['User-Agent'], 'ExampleAgent')
        self.assertEqual(ch.headers['Referer'], 'https://example.com/')
        self.assertEqual(ch.url, 'https://a.example/live.m3u8')

    def test_quoted_comma(self):
        attrs, name = bot.split_extinf('-1 tvg-name="A, B" group-title="Sports, Other",A, B')
        self.assertEqual(attrs['tvg-name'], 'A, B')
        self.assertEqual(attrs['group-title'], 'Sports, Other')
        self.assertEqual(name, 'A, B')

    def test_json_grouped(self):
        data = {"Turkey": [{"name": "Test TV", "url": "https://video.example/live.m3u8"}]}
        channels = bot.parse_json(json.dumps(data), 'https://www2.vavoo.to/')
        self.assertEqual(channels[0].group, 'Turkey')

    def test_output_m3u(self):
        ch = bot.Channel('Example TV', 'https://video.example/live.m3u8',
                         'TR', 'example.tr', 'https://example.com/logo.png',
                         headers={'User-Agent': 'ExampleAgent'})
        result = bot.output_m3u([ch], 'https://example.com/xmltv.xml')
        self.assertTrue(result.startswith('#EXTM3U url-tvg='))
        self.assertIn('tvg-id="example.tr"', result)
        self.assertIn('#EXTVLCOPT:http-user-agent=ExampleAgent', result)

    def test_only_unique_epg_ids(self):
        meta = bot.Metadata({'enabled': False}, 5)
        meta.by_name['sampletv'] = [('one.tv', 'TR', 'Sample TV'), ('two.tv', 'DE', 'Sample TV')]
        meta.logo_by_id['one.tv'] = 'https://img.example/one.png'
        tr = bot.Channel('Sample TV HD', 'https://video.example/a', group='Turkey')
        meta.enrich(tr, {})
        self.assertEqual(tr.tvg_id, 'one.tv')
        self.assertEqual(tr.logo, 'https://img.example/one.png')
        no_country = bot.Channel('Sample TV HD', 'https://video.example/b', group='Other')
        meta.enrich(no_country, {})
        self.assertEqual(no_country.tvg_id, '')


class TestValidation(unittest.TestCase):
    def test_private_urls_denied(self):
        self.assertFalse(bot.valid_http_url('http://127.0.0.1/config'))
        self.assertFalse(bot.valid_http_url('http://169.254.169.254/latest/meta-data'))
        self.assertFalse(bot.valid_http_url('http://localhost/'))
        self.assertFalse(bot.valid_http_url('ftp://a.example/'))
        self.assertTrue(bot.valid_http_url('https://video.example/a.m3u8'))

    def test_hls_segment_probe(self):
        def fake_bytes(url, headers, timeout, max_bytes, **kwargs):
            self.assertEqual(headers['User-Agent'], 'ExampleAgent')
            if url.endswith('master.m3u8'):
                return b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nmedia.m3u8\n', 'application/vnd.apple.mpegurl', url
            if url.endswith('media.m3u8'):
                return b'#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-MEDIA-SEQUENCE:2\n#EXTINF:8,\npart0.ts\n', 'application/vnd.apple.mpegurl', url
            if url.endswith('part0.ts'):
                return b'\x47' * 512, 'video/mp2t', url
            raise AssertionError(url)
        with patch.object(bot, 'get_bytes', side_effect=fake_bytes):
            check = bot.check_hls('https://video.example/master.m3u8', {'User-Agent': 'ExampleAgent'}, 3, True, False)
        self.assertEqual(check, 'hls_segment')

    def test_html_is_not_media(self):
        with patch.object(bot, 'get_bytes', return_value=(b'<html>not found</html>', 'text/html', 'https://video.example/test')):
            good, _ = bot.probe(bot.Channel('Bad TV', 'https://video.example/test'), 3, True, False)
        self.assertFalse(good)

    def test_failed_update_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'output').mkdir()
            playlist = root / 'output' / 'vavoo.m3u'
            playlist.write_text('PREVIOUS HEALTHY PLAYLIST', encoding='utf-8')
            config = {'source_url': 'https://source.example/index.m3u',
                      'metadata': {'enabled': False}}
            with patch.object(bot, 'get_bytes', side_effect=ConnectionError('offline')):
                with self.assertRaises(ConnectionError):
                    bot.build(config, root / 'config.json')
            self.assertEqual(playlist.read_text(), 'PREVIOUS HEALTHY PLAYLIST')

    def test_build_publishes_every_valid_channel_without_probing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'source_url': 'https://source.example/index.m3u', 'timeout_seconds': 4,
                      'workers': 2, 'metadata': {'enabled': False},
                      'probe_mode': 'url_change_only'}
            def fake_bytes(url, headers, timeout, max_bytes, **kwargs):
                if 'source.example' in url:
                    return EXAMPLE_M3U.encode(), 'application/x-mpegurl', url
                raise AssertionError(url)
            with patch.object(bot, 'get_bytes', side_effect=fake_bytes), \
                 patch.object(bot, 'probe', side_effect=AssertionError('Liveness probe must not run')):
                report = bot.build(config, root / 'config.json')
            self.assertEqual(report['published_channels'], 4)
            self.assertEqual(report['turkey_channels'], 2)
            self.assertEqual(report['url_new'], 4)
            self.assertEqual(report['health_checked_urls'], 0)
            result = (root / 'output' / 'vavoo.m3u').read_text(encoding='utf-8')
            self.assertEqual(result.count('#EXTINF:'), 4)
            self.assertIn('https://stream.example/live/trt.m3u8', result)
            self.assertIn('https://stream.example/live/trt-other.m3u8', result)
            self.assertIn('https://stream.example/broken.m3u8', result)
            turkiye = (root / 'output' / 'turkiye.m3u').read_text(encoding='utf-8')
            self.assertEqual(turkiye.count('#EXTINF:'), 2)
            self.assertNotIn('Sample TV', turkiye)
            self.assertEqual(len(json.loads((root / 'output' / 'vavoo.json').read_text())), 4)
            self.assertEqual(len(json.loads((root / 'output' / 'turkiye.json').read_text())), 2)

    def test_url_change_is_detected_without_dropping_other_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'source_url': 'https://source.example/index.m3u',
                      'metadata': {'enabled': False}}
            body = EXAMPLE_M3U
            with patch.object(bot, 'get_bytes', return_value=(body.encode(), 'text/plain', config['source_url'])):
                bot.build(config, root / 'config.json')
            # One duplicate source URL rotates; the other is unchanged.
            updated = body.replace('trt-other.m3u8', 'trt-new.m3u8')
            with patch.object(bot, 'get_bytes', return_value=(updated.encode(), 'text/plain', config['source_url'])):
                report = bot.build(config, root / 'config.json')
            self.assertEqual(report['published_channels'], 4)
            self.assertEqual(report['url_changed'], 1)
            self.assertEqual(report['url_unchanged'], 3)
            self.assertEqual(report['url_new'], 0)
            delta = json.loads((root / 'output' / 'url_changes.json').read_text())
            self.assertEqual(delta['changes'][0]['old_url'], 'https://stream.example/live/trt-other.m3u8')
            self.assertEqual(delta['changes'][0]['new_url'], 'https://stream.example/live/trt-new.m3u8')
            output = json.loads((root / 'output' / 'vavoo.json').read_text())
            self.assertEqual(sum(c['url_status'] == 'changed' for c in output), 1)
            self.assertEqual(len(output), 4)

    def test_reordered_alternatives_not_misreported_as_changed(self):
        before = [
            {'name': 'TRT', 'group': 'Turkey', 'url': 'https://a.example/a'},
            {'name': 'TRT', 'group': 'Turkey', 'url': 'https://a.example/b'}]
        now = [bot.Channel('TRT', 'https://a.example/b', 'Turkey'),
               bot.Channel('TRT', 'https://a.example/a', 'Turkey')]
        result = bot.compare_channel_urls(now, before)
        self.assertEqual(result['unchanged'], 2)
        self.assertEqual(result['changed'], 0)

    def test_removed_source_is_reported(self):
        before = [{'name': 'A', 'group': 'Turkey', 'url': 'https://a.example/1'},
                  {'name': 'B', 'group': 'Turkey', 'url': 'https://a.example/2'}]
        now = [bot.Channel('A', 'https://a.example/1', 'Turkey')]
        result = bot.compare_channel_urls(now, before)
        self.assertEqual(result['removed'], 1)
        self.assertEqual(result['removed_entries'][0]['name'], 'B')

    def test_turkish_country_detection(self):
        examples = [bot.Channel('TRT 1', 'https://a.example/a', 'Turkey'),
                    bot.Channel('TRT 1', 'https://a.example/b', 'Türkiye'),
                    bot.Channel('TRT 1', 'https://a.example/c', 'TR | Spor'),
                    bot.Channel('TRT 1', 'https://a.example/d', 'Misc', country='TR')]
        self.assertTrue(all(bot.infer_country(ch) == 'TR' for ch in examples))

    def test_health_probe_is_report_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'source_url': 'https://source.example/index.m3u',
                      'metadata': {'enabled': False}, 'probe_mode': 'health_report_only'}
            def fake_probe(ch, timeout, check_segments, allow_private):
                return (False, 'HTTP 404') if 'broken' in ch.url else (True, 'mock_live')
            with patch.object(bot, 'get_bytes', return_value=(EXAMPLE_M3U.encode(), 'text/plain', config['source_url'])), \
                 patch.object(bot, 'probe', side_effect=fake_probe):
                report = bot.build(config, root / 'config.json')
            self.assertEqual(report['health_failed'], 1)
            self.assertEqual(report['published_channels'], 4)
            self.assertEqual(report['not_removed_due_to_failed_probe'], 1)

    def test_partial_source_does_not_overwrite_previous_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {'source_url': 'https://source.example/index.m3u',
                   'metadata': {'enabled': False},
                   'minimum_source_ratio': 0.8}
            with patch.object(bot, 'get_bytes', return_value=(EXAMPLE_M3U.encode(), 'text/plain', cfg['source_url'])):
                bot.build(cfg, root / 'config.json')
            previous = (root / 'output' / 'vavoo.m3u').read_text()
            reduced = EXAMPLE_M3U.split('#EXTINF:-1 group-title="Germany",Sample TV')[0]
            with patch.object(bot, 'get_bytes', return_value=(reduced.encode(), 'text/plain', cfg['source_url'])):
                with self.assertRaisesRegex(RuntimeError, 'Kaynak aniden küçüldü'):
                    bot.build(cfg, root / 'config.json')
            self.assertEqual((root / 'output' / 'vavoo.m3u').read_text(), previous)


if __name__ == '__main__':
    unittest.main()

# VAVOO Tam Liste + Türkiye + URL Değişim Takibi

Bu bot her 4 saatte bir VAVOO kaynak listesini **yeniden indirir** ve kaynaktaki
**geçerli HTTP/HTTPS yayın adreslerinin tamamını** olduğu gibi yayınlar.
Yayın yavaş açılıyor, timeout veriyor veya ilk segmenti yüklenmiyor diye kanal **silinmez**.
Aynı isimli kanalın birden fazla yayın adresi varsa **hepsi ayrı kayıt** olarak kalır.

## Ne yapıyor?

1. `config.json` içindeki `source_url` adresinden güncel M3U/JSON kaynağını indirir.
2. Kanal adı, grup, URL, header, logo ve EPG alanlarını okur ve zenginleştirir.
3. **Yalnızca URL değişimini kontrol eder:** Önceki çalışmadaki `output/vavoo.json`
   ile aynı grup+isim altındaki adresleri karşılaştırır; `new`, `unchanged`, `changed`
   olarak kaydeder. Önceki kaynakta olup güncel kaynakta olmayanlar da `removed` raporuna yazılır.
4. Tüm dünya kanallarını `output/vavoo.m3u` ve `output/vavoo.json` dosyasına yazar.
5. `Turkey`, `Türkiye`, `TR` veya `tvg-country="TR"` olarak tanınan kanalların
   **tamamını** ayrıca `output/turkiye.m3u` ve `output/turkiye.json` dosyasına yazar.
6. Değişen/eski/yeni adres sayılarını `output/status.json`, ayrıntılı değişimleri
   `output/url_changes.json` dosyasına kaydeder.

### Önemli fark

Varsayılan `probe_mode: "url_change_only"` **canlı yayın oynatma veya bağlantı
sağlık testi yapmaz**; eski ve yeni URL metinlerini karşılaştırır. Böylece
başarısız/timeout olan yayınlar asla filtrelenmez.
Kaynağın aynı URL'ye yönlendirdiği farklı son sunucu adreslerini tespit etmez.
Kaynak kanalın URL'sini değiştirdiyse yeni URL'yi otomatik yayınlar.
**Kaynakta hiç bulunmayan bir kanalı bot kendiliğinden ekleyemez.**

İsteğe bağlı `probe_mode: "health_report_only"` yapılırsa yayın bağlantı testi
çalışır, ancak **başarısız test sonucuyla hiçbir kanal listeden çıkarılmaz**.
Bu mod 10.000 yayında çok uzun sürebilir. `check_hls_segments: false` bunun
altında HLS segment testini kapatır; HLS listesi kontrolü kalabilir.

## Çıktı dosyaları

| Dosya | Açıklama |
|---|---|
| `output/vavoo.m3u` | Kaynaktaki tam liste (tekrarlı adlar dahil) |
| `output/vavoo.json` | Tam liste, URL durumu ve kanal bilgileri |
| `output/turkiye.m3u` | Kaynaktaki **tüm** Türkiye yayınları |
| `output/turkiye.json` | Türkiye yayınlarının JSON listesi |
| `output/url_changes.json` | Değişen URL'lerin eski/yeni halleri, kaybolanlar |
| `output/status.json` | Toplam/Türkiye sayısı ve URL değişim istatistikleri |

**Not:** Önceki sürümden kalan 4.297 kanallı dosyalar bu ZIP'e konmadı.
İlk çalıştırmada yeni tam kaynak listesi oluşturulur. Eski repo üzerine sadece
kodları atarsanız eski çıktılar da ilk başarılı çalışmada yenilenir.

## Kullanım

Python 3.11+ kurulu olmalı:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python vavoo_bot.py
```

Windows: `calistir.bat` dosyasını açın.
GitHub: `.github/workflows/vavoo-update.yml` otomatik her 4 saatte bir
çalışır; **Actions > Vavoo - 4 Saatlik M3U > Run workflow** üzerinden
hemen de çalıştırılabilir. Çıktı dosyalarını otomatik commit/push yapar.

## Koruma ayarları

- `minimum_source_channels`: kaynak bariz boşsa eski listeyi korur.
- `minimum_source_ratio`: önceki kaynağa göre kanal sayısı büyük oranda düştüyse
  hatalı/kırpılmış yanıtın yayınlanmasını durdurur.
- `minimum_turkey_channels`: Türkiye grubu tamamen kaybolursa eski listeyi korur.
- `minimum_turkey_ratio`: önceki başarılı çalışmada bilinen Türkiye sayısı
  aniden büyük oranda düşerse yeni çıktıyı yayınlamaz.

Bu önlemler kaynak hatası nedeniyle var olan listenin üzerine eksik liste
yazılmasını önlemeye yardımcı olur. Sadece **gerçek kaynaktan** gelen adresler
üretilebilir; erişim gerektiren yayınların erişim kontrolleri aşılmaz.

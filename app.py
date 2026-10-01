import streamlit as st
import pandas as pd
from PIL import Image
import io
import fitz
import os
import json
import google.generativeai as genai
import typing_extensions as typing

# --- GEMINI API AYARLARI (GÜVENLİ KASA) ---
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
genai.configure(api_key=GEMINI_API_KEY)

# --- OTOMATİK MODEL BULUCU ---
try:
    _aktif_modeller = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
    if _aktif_modeller:
        _secili_model = _aktif_modeller[0]
        for ideal_isim in ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro"]:
            bulundu_mu = False
            for m in _aktif_modeller:
                if ideal_isim in m:
                    _secili_model = m
                    bulundu_mu = True
                    break
            if bulundu_mu:
                break
        AKTIF_MODEL = _secili_model
    else:
        AKTIF_MODEL = "gemini-1.5-pro"
except Exception:
    AKTIF_MODEL = "gemini-1.5-pro"

# --- ARAYÜZ DÜZENLEMESİ ---
st.set_page_config(page_title="Faturadan Excele - KDV Listesi Oluşturucu", layout="wide", page_icon="📑")
st.title("📑 Faturadan Excele Dönüştürücü")
st.markdown("Yapay Zeka Destekli **KDV İade & Tam Tasdik** Listesi Oluşturucu")

# --- YAPILANDIRILMIŞ ÇIKTI ŞEMASI ---
class FaturaVerisi(typing.TypedDict):
    unvan: str
    vkn_tckn: str
    mal_cinsi: str
    matrah: float
    kdv: float
    fatura_no: str
    tarih: str

class FaturaListesi(typing.TypedDict):
    faturalar: list[FaturaVerisi]

def fatura_satirini_olustur(sira_no, ai_verisi):
    return {
        "Sıra No": sira_no,
        "Alış Faturasının Tarihi": str(ai_verisi.get("tarih", "")),
        "Alış Faturasının Serisi": "",
        "Alış Faturasının Sıra No'su": str(ai_verisi.get("fatura_no", "")).upper(),
        "Satıcının Adı-Soyadı/Ünvanı": str(ai_verisi.get("unvan", "")).upper(), 
        "Satıcının Vergi Kimlik Numarası/TC Kimlik Numarası": str(ai_verisi.get("vkn_tckn", "")),
        "Alınan Mal ve/veya Hizmetin Cinsi": str(ai_verisi.get("mal_cinsi", "")).upper(),
        "Alınan Mal ve/veya Hizmetin Miktarı": "1",
        "Alınan Mal ve/veya Hizmetin KDV Hariç Tutarı": ai_verisi.get("matrah", 0.0),
        "KDV'si": ai_verisi.get("kdv", 0.0)
    }

# --- KVKK VE GÜVENLİK ALANI ---
st.info("🔒 **Sıfır Veri Saklama Taahhüdü:** Yüklediğiniz fatura PDF'leri sunucu diskine kaydedilmez, anlık olarak bellekte (RAM) işlenir. Excel çıktısı oluşturulduktan hemen sonra veriler kalıcı olarak yok edilir.")

# --- ANA İŞLEM ALANI ---
uploaded_file = st.file_uploader("Fatura PDF'ini Sürükle veya Seç", type=["pdf"], help="Çok sayfalı fatura PDF'inizi buraya yükleyin.")

if uploaded_file is not None:
    if st.button("KDV İade Listesine Çevir 🚀", use_container_width=True):
        with st.spinner("PDF sayfaları görsele çevriliyor ve inceleniyor..."):
            nihai_liste = []
            try:
                # Bellek içi PDF okuma (Diske yazılmaz, KVKK dostu)
                doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
                progress_bar = st.progress(0)
                status_text = st.empty()
                toplam_sayfa = len(doc)
                
                fatura_gorselleri = []
                for page_num in range(toplam_sayfa):
                    status_text.text(f"Görsel Hazırlanıyor: Sayfa {page_num + 1} / {toplam_sayfa}")
                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(dpi=300)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    fatura_gorselleri.append(img)
                    progress_bar.progress((page_num + 1) / toplam_sayfa)

                status_text.text("Faturalar inceleniyor ve matrah hesapları yapılıyor... (10-30 sn)")
                
                prompt = """
                Sen KDV İade ve Tam Tasdik raporları denetimi yapan uzman bir Yeminli Mali Müşavirsin.
                Ekteki görseller, çok sayfalı bir PDF belgesinin sayfalarıdır. Belge içinde birden fazla fatura bulunmaktadır.

                GÖREVİN:
                Tüm görselleri bir bütün olarak incele, geçerli olan HER BİR FATURAYI tespit et ve liste olarak döndür.

                HAYATİ KURALLAR:
                1. SİLİK YAZILAR (OKUNAMAYANLAR): Faturaların bazıları çok silik olabilir. Faturayı sakın atlama! Okuyamadığın metin bilgileri için (Unvan, VKN, Tarih vb.) boş bırakmak yerine mutlaka "OKUNAMADI" yaz.
                2. İKİ SAYFALIK FATURALAR: Eğer bir fatura iki veya daha fazla sayfaya yayılmışsa, bunları zekanla birleştirip TEK BİR FATURA satırı olarak listeye ekle.
                3. ÇÖP SAYFALAR: Sadece karekod içeren veya boş olan sayfaları faturaymış gibi listeye ekleme.
                4. MATRAH (KDV HARİÇ TUTAR) MANTIĞI KONTROLÜ: "Matrah" faturadaki KDV'nin hesaplandığı BAZ tutardır. Faturadaki "KDV Matrahı", "Mal ve Hizmet Toplam Tutarı" veya "KDV Hariç Tutar" karşısındaki rakamı tespit etmelisin. KESİNLİKLE "Ödenecek Tutar", "Vergiler Dahil Toplam Tutar" veya "Genel Toplam" (KDV Dahil) rakamını matrah kısmına YAZMA. Mantıksız ve tutarsız rakamlar yazmaktansa, faturada açıkça belirtilen KDV matrahı tutarını bularak yaz.

                ÇEKİLECEK VERİLER:
                - unvan: Faturayı kesen satıcının Unvanı. (Silikse "OKUNAMADI" yaz).
                - vkn_tckn: 10 haneli VKN veya 11 haneli TCKN. (Silikse "OKUNAMADI" yaz).
                - mal_cinsi: Genel kategori (Örn: "LOJİSTİK", "MUHASEBE ÜCRETİ").
                - matrah: KDV Hariç Tutar / KDV Matrahı (Sadece rakam).
                - kdv: KDV Tutarı (Sadece rakam, okuyamazsan 0.0).
                - fatura_no: 16 haneli fatura numarası. (Silikse "OKUNAMADI" yaz).
                - tarih: GG.AA.YYYY formatında tarih. (Silikse "OKUNAMADI" yaz).
                """

                model = genai.GenerativeModel(AKTIF_MODEL)
                
                response = model.generate_content(
                    [prompt] + fatura_gorselleri,
                    generation_config=genai.GenerationConfig(
                        response_mime_type="application/json",
                        response_schema=FaturaListesi,
                        temperature=0.0
                    )
                )
                
                veri = json.loads(response.text)
                bulunan_faturalar = veri.get("faturalar", [])
                
                sira_no = 1
                for fatura in bulunan_faturalar:
                    nihai_liste.append(fatura_satirini_olustur(sira_no, fatura))
                    sira_no += 1

            except Exception as e:
                st.error(f"İşlem sırasında beklenmeyen bir hata oluştu. Hata Detayı: {e}")
                st.stop()
            
            if nihai_liste:
                df = pd.DataFrame(nihai_liste)
                st.success("İşlem Başarılı! Faturalar ayrıştırıldı ve Excel hazırlandı.")
                st.dataframe(df)
                
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                    
                col1, col2 = st.columns(2)
                with col1:
                    st.download_button("📥 Excel Dosyasını İndir", buffer.getvalue(), "kdv_iade_listesi.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
                with col2:
                    if st.button("🔄 Yeni Dosya Yükle / Ekranı Temizle", use_container_width=True):
                        st.rerun()
            else:
                st.warning("Hiçbir fatura verisi tespit edilemedi.")

# --- SİDEBAR BİLGİLENDİRME ---
st.sidebar.title("Kullanım Bilgileri")
st.sidebar.markdown("""
### Ücretsiz Kota
Günlük **ücretsiz kullanım hakkınız** bulunmaktadır. Sistemin yoğunluğuna göre işlem süreleri 10 ile 30 saniye arası sürebilir.

### Gizlilik ve Güvenlik
- 🔒 Dosyalarınız bellekte anlık işlenir.
- 🗑️ İşlem bitince hemen silinir.
- 🛡️ KVKK standartlarına uygundur.
""")
st.sidebar.markdown("---")
st.sidebar.caption("© 2024 FaturaExcel.com.tr - Tüm Hakları Saklıdır.")
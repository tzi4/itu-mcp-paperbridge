"""Routing metadata for every tool: its domain, effect and search keywords.

Domain names deliberately match the upstream ``toolsets`` groups (plus
``papers`` for PaperBridge) so the same vocabulary works in both projects.
Keywords are search data, not prose: most users write in Turkish, so each
entry lists the Turkish words a student would use next to the English ones.
``tests/test_tool_router.py`` checks that every registered tool has a domain.
"""

from __future__ import annotations

# name -> (one-line description, tool names). Order is the display order.
DOMAINS: dict[str, tuple[str, tuple[str, ...]]] = {
    "core": (
        "Login, session and service health diagnostics",
        ("auth_status", "refresh_session", "obs_auth_status", "itu_health"),
    ),
    "ninova": (
        "Ninova LMS courses: announcements, files, assignments, LMS grades, message board, "
        "attendance, online lessons, change tracking and deadlines",
        (
            "get_dashboard", "list_courses", "get_left_courses", "get_course_announcements",
            "get_course_class_files", "get_course_lesson_files", "get_course_assignments",
            "get_course_info", "get_course_sections", "get_course_grades",
            "get_course_message_board", "get_course_message_thread",
            "ninova_prepare_message_reply", "ninova_post_message_reply",
            "get_course_attendance", "get_course_remote_learning", "get_course_overview",
            "get_dashboard_announcements", "get_dashboard_assignments", "sync_all_courses",
            "get_updates", "get_upcoming_deadlines", "get_assignment_upload_slots",
            "submit_assignment",
        ),
    ),
    "files": (
        "Ninova pages and files: read, crawl, download, text extraction, snapshots",
        ("read_page", "crawl_course", "download_resource", "read_resource_text",
         "snapshot_page", "diff_snapshot"),
    ),
    "obs": (
        "OBS student record: profile, official grades, weekly schedule, attendance, graduation, transcript",
        (
            "obs_get_profile", "obs_list_programs", "obs_list_semesters",
            "obs_get_grade_distribution", "obs_get_advisor", "obs_get_internships",
            "obs_get_contacts", "obs_list_registered_courses", "obs_get_course_grades",
            "obs_get_attendance", "obs_get_schedule", "obs_get_graduation_remaining",
            "obs_download_transcript", "get_personal_exam_calendar",
        ),
    ),
    "registration": (
        "OBS course registration: status, saved draft, elective groups, plan check",
        ("obs_get_registration_status", "obs_get_registration_draft",
         "obs_save_registration_draft", "obs_get_elective_group",
         "obs_validate_registration_plan"),
    ),
    "portal": (
        "Portal (login): campus card, cafeteria menu, notifications, reading and opening help tickets, cloud and mail quota",
        ("obs_get_campus_card", "obs_get_notifications", "obs_get_help_tickets",
         "get_help_ticket", "help_ticket_options", "help_prepare_ticket", "help_submit_ticket",
         "obs_get_cloud_quota", "get_cafeteria_menu"),
    ),
    "planning": (
        "Planning: GPA, target GPA, relative grade, conflicts, degree plan, eligibility",
        ("obs_calculate_gpa", "calculate_target_gpa", "estimate_relative_grade",
         "check_course_conflicts", "list_degree_faculties", "list_degree_programs",
         "build_degree_plan", "explain_course_eligibility", "plan_remaining_courses",
         "export_calendar"),
    ),
    "public": (
        "Public OBS catalog: course search, section schedules, prerequisites, exams, academic calendar, rooms",
        ("obs_search_courses", "obs_get_course_prerequisites", "get_academic_calendar",
         "get_public_course_schedule", "get_public_course_prerequisites",
         "get_public_exam_schedule", "find_open_course_sections", "find_empty_classrooms"),
    ),
    "coursehub": (
        "Official course sites: mathematics, physics, chemistry, laboratory guidance, YDY language programs, material index, student accounts and reviewed forms",
        ("coursehub_sources", "coursehub_explore", "coursehub_materials", "coursehub_page", "coursehub_text",
         "coursehub_review_change", "coursehub_commit_change"),
    ),
    "campus": (
        "Campus life, no login: shuttle, directory, buildings, sports, announcements",
        ("search_itu_directory", "get_shuttle_schedule", "search_campus_locations",
         "get_sports_facility_hours", "get_itu_announcements"),
    ),
    "library": (
        "Library: public catalog search, records and copy availability",
        ("library_search", "library_get_item", "library_check_availability"),
    ),
    "archive": (
        "Course archive since 2016: past sections, instructors, fill rates, term comparison",
        ("archive_list_terms", "archive_course_history", "archive_who_taught",
         "archive_instructor_courses", "archive_term_sections", "archive_fill_rate",
         "archive_search_courses", "archive_list_branches", "archive_compare_terms"),
    ),
    "hocametre": (
        "HocaMetre: anonymous student ratings of instructors from notkutusu.com, per instructor or per course section",
        ("hocametre_instructor", "hocametre_course_sections"),
    ),
    "mail": (
        "ITU Mail: inbox, sent messages, search, message bodies, conversations, attachments, sending, forwarding "
        "or saving a reviewed message to Drafts",
        ("mail_status", "mail_list_inbox", "mail_list_sent", "mail_get_message",
         "mail_get_thread", "mail_get_attachment", "mail_search", "mail_prepare_message", "mail_send_message",
         "mail_save_draft"),
    ),
    "papers": (
        "PaperBridge research papers: search, resolve, full text and page views through institutional access",
        ("papers_search", "papers_resources", "papers_library_query", "papers_resolve", "papers_get_text", "papers_get_page",
         "papers_status", "search", "fetch", "papers_view_page", "papers_view_pages",
         "papers_visual_probe"),
    ),
}

TOOL_DOMAIN: dict[str, str] = {
    tool: domain for domain, (_, tools) in DOMAINS.items() for tool in tools
}

# Operations that change the user's data in an ITU service, or prepare such a
# change for confirmation. Everything else reads; a read may still cache or
# save a file locally. ``tests/test_tool_router.py`` checks this against the
# server's stateful and destructive tool sets.
WRITE_OPERATIONS: frozenset[str] = frozenset({
    "submit_assignment",
    "obs_save_registration_draft",
    "mail_prepare_message", "mail_send_message", "mail_save_draft",
    "help_prepare_ticket", "help_submit_ticket",
    "ninova_prepare_message_reply", "ninova_post_message_reply",
    "coursehub_review_change", "coursehub_commit_change",
})
EFFECTS = ("read", "write")


def effect(name: str) -> str:
    """``write`` for an operation that changes ITU data, otherwise ``read``."""
    return "write" if name in WRITE_OPERATIONS else "read"

# Extra search words per tool, Turkish first. Names, titles and descriptions
# are indexed as well, so list only what a student would type but the
# description does not already say.
KEYWORDS: dict[str, str] = {
    "coursehub_sources": "havuz matematik fizik kimya laboratuvar FIZLAB KIMLAB yabancı dil YDY mathavuz fizikhavuz kimyahavuz",
    "coursehub_explore": "havuz duyuru izlence ders notu sınav takvimi laboratuvar deney hazırlık İngilizce seçmeli diller YDY MAT FIZ KIM ING ALM ARB CIN FRA ISP ITA JPN RUS YTO KOR",
    "coursehub_materials": "havuz materyal listesi ders sunuları slayt çalışma soruları sınav arşivi izlence rehber laboratuvar deney föyü hazırlık İngilizce seçmeli dil YDY materials index slides practice worksheets exams syllabi guides",
    "coursehub_page": "havuz sınav yeri sonuç itiraz sunu arşiv öğrenci hesabı laboratuvar deney kılavuz YDY hazırlık dil programı",
    "coursehub_text": "havuz pdf slayt sunu pptx konuşmacı notları belge laboratuvar deney föyü İngilizce YDY dil katalog",
    "coursehub_review_change": "havuz işlem önizleme itiraz form",
    "coursehub_commit_change": "havuz onayla form gönder",
    "auth_status": "giriş durumu oturum login kontrol",
    "refresh_session": "oturumu yenile tekrar giriş",
    "obs_auth_status": "obs giriş durumu",
    "itu_health": "sağlık durumu servisler çalışıyor mu erişilebilir mi gecikme yazma izinleri sürüm giriş health status latency reachable uptime write capability version auth rate limit",
    "get_dashboard": "genel durum özet ana sayfa derslerim ödevlerim duyurular overview",
    "list_courses": "derslerim ders listesi kayıtlı olduğum dersler ninova dersleri",
    "get_course_announcements": "ders duyurusu duyurular hoca duyurusu",
    "get_course_class_files": "sınıf dosyaları ders notları slayt pdf dosya",
    "get_course_lesson_files": "ders dosyaları ders notları slayt pdf dosya",
    "get_course_assignments": "ödev ödevler teslim tarihi proje homework",
    "get_course_info": "ders bilgisi sınıf bilgileri hoca eğitmen asistan ders saati derslik crn syllabus haftalık plan kaynak kitap",
    "get_course_sections": "ders bölümleri sekmeler",
    "get_course_grades": "ninova notu not notlarım vize final hocanın girdiği not lms grade puan sınıf ortalaması yüzde ağırlık",
    "get_course_message_board": "mesaj panosu forum soru cevap başlıklar konular tartışma",
    "get_course_message_thread": "mesaj panosu mesajı oku konu başlık cevaplar yazışma forum mesajları",
    "ninova_prepare_message_reply": "mesaj panosuna cevap yaz yanıtla cevapla hocaya soru sor foruma yaz mesaj taslağı",
    "ninova_post_message_reply": "cevabı gönder mesajı gönder yanıtı onayla mesaj panosuna gönder",
    "get_course_attendance": "ninova yoklama devamsızlık kepler katılım",
    "get_course_remote_learning": "uzaktan eğitim ders kaydı video canlı ders online ders zoom bağlantı link kayıt şifre",
    "get_left_courses": "ayrıldığım sınıflar ayrıldığım dersler eski dersler kaybolan ders listede yok",
    "get_course_overview": "ders özeti tek ders hepsi bir arada",
    "get_dashboard_announcements": "son duyurular yeni duyuru tüm derslerin duyuruları",
    "get_dashboard_assignments": "tüm ödevler açık ödevler bekleyen ödevler",
    "sync_all_courses": "tüm dersleri senkronize et güncelle",
    "get_updates": "ne değişti yenilikler yeni dosya yeni duyuru değişiklik",
    "get_upcoming_deadlines": "yaklaşan teslim tarihleri son tarih bu hafta ödev deadline",
    "get_assignment_upload_slots": "ödev yükleme alanı teslim dosyası",
    "submit_assignment": "ödev teslim et yükle gönder",
    "read_page": "sayfayı oku ninova sayfası",
    "crawl_course": "dersin tüm sayfaları tara",
    "download_resource": "dosya indir",
    "read_resource_text": "dosyayı oku pdf oku metin çıkar özetle",
    "snapshot_page": "sayfa kaydı anlık görüntü",
    "diff_snapshot": "sayfa farkı ne değişti",
    "obs_get_profile": "öğrenci bilgileri numara bölüm sınıf kaçıncı sınıf profil",
    "obs_list_programs": "programlarım bölümüm çift anadal yandal",
    "obs_list_semesters": "dönemlerim yarıyıllar",
    "obs_get_grade_distribution": "not dağılımı sınıf ortalaması harf notu dağılımı",
    "obs_get_advisor": "danışman danışmanım akademik danışman hoca iletişim",
    "obs_get_internships": "staj stajlarım staj durumu",
    "obs_get_contacts": "iletişim bilgilerim telefon e-posta adres",
    "obs_list_registered_courses": "kayıtlı derslerim bu dönem aldığım dersler crn",
    "obs_get_course_grades": "obs notu resmi not harf notu vize final notlarım açıklandı mı official grade",
    "obs_get_attendance": "devamsızlık yoklama devam durumu sınır obs",
    "obs_get_schedule": "ders programı haftalık program derslik sınıf saat bugün yarın nerede hangi sınıfta",
    "obs_get_graduation_remaining": "mezuniyet kalan dersler mezun olmak için kaç ders",
    "obs_download_transcript": "transkript indir not dökümü",
    "get_personal_exam_calendar": "sınavlarım sınav takvimi final vize sınav tarihi exam",
    "obs_get_registration_status": "kayıt durumu ders kaydı açık mı kayıt tarihleri",
    "obs_get_registration_draft": "ders seçim taslağı kayıt taslağı",
    "obs_save_registration_draft": "taslağı kaydet ders seçimi kaydet",
    "obs_get_elective_group": "seçmeli ders grubu seçmeli havuzu",
    "obs_validate_registration_plan": "ders planımı kontrol et çakışma ön şart kontenjan",
    "obs_get_campus_card": "kampüs kartı bakiye para yemek kartı kart",
    "obs_get_notifications": "bildirimler portal bildirimi",
    "obs_get_help_tickets": "destek talepleri yardım bileti çağrı",
    "get_help_ticket": "destek talebi ayrıntı",
    "help_ticket_options": "yardım bileti aç birim kategori nereye yazmalıyım",
    "help_prepare_ticket": "yardım bileti oluştur destek talebi aç şikayet bildir",
    "help_submit_ticket": "bileti gönder destek talebini onayla",
    "obs_get_cloud_quota": "bulut kotası depolama mail kotası",
    "get_cafeteria_menu": "yemekhane menü yemek öğle akşam bugün ne var",
    "obs_calculate_gpa": "not ortalamam gno ano ortalama gpa hesapla",
    "calculate_target_gpa": "hedef ortalama kaç almam gerekir ortalamayı yükselt",
    "estimate_relative_grade": "bağıl not tahmini çan eğrisi harf notu tahmin",
    "check_course_conflicts": "ders çakışması saat çakışıyor mu",
    "list_degree_faculties": "fakülteler",
    "list_degree_programs": "lisans programları bölümler",
    "build_degree_plan": "ders planı müfredat dönem dönem dersler",
    "explain_course_eligibility": "alabilir miyim ön şart neden alamıyorum",
    "plan_remaining_courses": "kalan dersleri planla gelecek dönem ne almalıyım",
    "obs_search_courses": "ders ara ders kodu ders adı katalog",
    "obs_get_course_prerequisites": "ön şart önkoşul",
    "export_calendar": "takvim dışa aktar ics ders programını takvime ekle telefon takvimi google calendar sınav ödev",
    "get_academic_calendar": "akademik takvim final haftası tatil dönem başlangıcı ekle bırak",
    "get_public_course_schedule": "ders şubeleri crn gün saat derslik hoca bu dönem açılan",
    "get_public_course_prerequisites": "ön şart önkoşul",
    "get_public_exam_schedule": "sınav programı final takvimi genel sınav",
    "find_open_course_sections": "kontenjan boş yer açık şube",
    "find_empty_classrooms": "boş derslik boş sınıf çalışma yeri şu an",
    "search_itu_directory": "rehber telefon hoca e-posta personel",
    "get_shuttle_schedule": "ring servis otobüs saatleri",
    "search_campus_locations": "bina nerede kampüs haritası konum",
    "get_sports_facility_hours": "spor salonu havuz saatleri",
    "get_itu_announcements": "itü duyuruları üniversite duyurusu haber",
    "library_search": "kütüphane kitap ara katalog",
    "library_get_item": "kitap ayrıntısı",
    "library_check_availability": "kitap rafta mı ödünç alınabilir mi müsait",
    "archive_list_terms": "arşiv dönemleri",
    "archive_course_history": "dersin geçmişi hangi dönem açılıyor",
    "archive_who_taught": "dersi kim verdi geçmiş hocalar eğitmen",
    "archive_instructor_courses": "hocanın verdiği dersler",
    "archive_term_sections": "geçmiş dönem şubeleri",
    "archive_fill_rate": "doluluk oranı kontenjan doluluğu",
    "archive_search_courses": "arşivde ders ara",
    "archive_list_branches": "arşiv bölüm kodları",
    "archive_compare_terms": "iki dönemi karşılaştır",
    "hocametre_instructor": "hocametre notkutusu hoca yorumları hocası nasıl hoca puanı değerlendirme iyi mi not veriyor mu yoklama alıyor mu",
    "hocametre_course_sections": "hocametre notkutusu hangi hocadan almalıyım hangi şube hoca seçimi şubeleri hocalara göre karşılaştır hoca puanları crn pick choose best instructor section rating",
    "mail_status": "mail durumu okunmamış sayısı",
    "mail_list_inbox": "gelen kutusu maillerim e-postalar okunmamış email inbox",
    "mail_list_sent": "gönderilen mailler giden kutusu",
    "mail_get_message": "maili oku e-posta içeriği",
    "mail_get_thread": "mail yazışması konuşma zinciri cevap geldi mi yanıt verdi mi hoca cevap yazdı mı thread reply conversation replied answered answer email instructor",
    "mail_get_attachment": "mail eki ek dosya",
    "mail_search": "mail ara eski mailler e-posta arama kimden gelen email find",
    "mail_prepare_message": "mail yaz e-posta taslağı hocaya mail at yanıtla cevap ver maili ilet yönlendir forward email write compose instructor lecturer",
    "mail_send_message": "maili gönder e-posta gönder onayla email send",
    "mail_save_draft": "taslağa kaydet taslaklar taslak olarak kaydet göndermeden kaydet webmail",
    "papers_search": "makale makaleler ara bul akademik yayın literatür araştırma güncel yıl tarih atıf find article paper scholarly research literature discovery crossref openalex citation recent year publication",
    "papers_resources": "research resources databases institutional entry subjects directory library İTÜ kütüphane veritabanı veritabanları veritabanlarını araştırma kaynak kaynakları konu konular abonelik",
    "papers_library_query": "ProQuest Central subscription literature discovery journal dissertation thesis scholarly records search proquest kurumsal abonelik makale tez tezler dergi ara tarama",
    "papers_resolve": "doi çözümle erişim bağlantısı yayıncı link resolve identifier access lookup",
    "papers_get_text": "makale tam metin oku",
    "papers_get_page": "makale sayfası",
    "papers_status": "paperbridge durumu",
    "search": "research compatibility deep research alias",
    "fetch": "makale getir",
}

# Operations whose parameters are all optional in the schema but which need one
# of these groups to run. Each group is complete when all its members are given.
REQUIRED_ONE_OF: dict[str, tuple[tuple[str, ...], ...]] = {
    "obs_get_course_grades": (("class_id",), ("course",)),
    "obs_get_attendance": (("class_id",), ("course",)),
    "get_assignment_upload_slots": (("upload_url",), ("course", "assignment")),
    "archive_fill_rate": (("crn",), ("course_code",)),
}

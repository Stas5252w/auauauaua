from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Списки в .env задаются через запятую: OWNER_IDS=123,456
StrList = Annotated[list[str], NoDecode]
IntList = Annotated[list[int], NoDecode]

DEFAULT_PROFILE = (
    "Студия делает: сайты и лендинги под ключ, Telegram-боты, ИИ-автоматизацию бизнеса "
    "(чат-боты, интеграции, CRM, n8n), ИИ-мультики и видеоролики. Работаем удалённо по всей России и СНГ."
)

DEFAULT_PHRASES = [
    "нужен сайт", "ищу кто сделает сайт", "нужен telegram бот", "ищу разработчика бота",
    "нужна автоматизация", "ищу кто сделает мультик", "нужен ролик нейросеть", "посоветуйте кто делает сайты",
    "нужен лендинг", "ищу веб разработчика", "кто делает чат ботов", "нужна нейросеть для бизнеса",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_ids: IntList = Field(default_factory=list)
    db_path: str = "data/leads.db"
    score_threshold: int = 5
    max_age_hours: int = 48  # старше — не лид, уже разобрали
    profile: str = DEFAULT_PROFILE  # что продаём — идёт в промпт ИИ; меняется через /profile
    search_phrases: StrList = Field(default_factory=lambda: list(DEFAULT_PHRASES))

    # «Человеческий» режим для источников без API: прокси, рабочие часы, браузерные профили
    proxy_url: str | None = None  # http://user:pass@host:port — лучше резидентный/мобильный
    work_hours: str = "8-23"
    browser_profiles_dir: str = "data/profiles"
    browser_headless: bool = True

    # Threads без API (поиск без логина). Если задан THREADS_ACCESS_TOKEN — используется официальный API
    threads_web_enabled: bool = True

    # Биржи без логина
    kwork_enabled: bool = True
    kwork_categories: StrList = Field(default_factory=lambda: ["11"])  # 11 = Разработка и IT; "all" = все категории (много мусора)
    kwork_pages: int = 3
    kwork_interval: int = 600
    weblancer_enabled: bool = True
    weblancer_urls: StrList = Field(default_factory=lambda: ["https://www.weblancer.net/jobs/"])
    weblancer_interval: int = 900
    freelance_ru_enabled: bool = True
    freelance_ru_queries: StrList = Field(default_factory=lambda: ["сайт", "лендинг", "бот", "автоматизация", "нейросеть", "мультик"])
    freelance_ru_interval: int = 900

    # Яндекс.Карты без API + аудит сайтов организаций (мёртвый / без HTTPS / не мобильный → лид на редизайн)
    yandex_web_enabled: bool = True
    site_audit_enabled: bool = True
    site_audit_per_poll: int = 30

    # Instagram через залогиненный браузерный профиль (python -m leadhunter.browser.login instagram)
    ig_web_enabled: bool = False
    ig_web_per_poll: int = 3

    # Отправка черновика клиенту через ваш Telegram-аккаунт: лимит в день (0 = выключено)
    tg_outreach_limit: int = 0

    # Вебхук для CRM / n8n / Google Sheets: POST JSON с каждым лидом
    webhook_url: str | None = None

    # Рабочая группа и команда
    hot_score: int = 8  # с этого балла лид идёт в «🔥 Горячие» со звуком
    deliver_dm: bool = False  # дублировать лидов владельцам в личку, даже если настроена группа
    sla_minutes: int = 10  # лид не взят столько минут → пинг в «Горячие»
    seen_ttl_days: int = 30  # дедуп-таблицы (seen, хэши, события авторов) старше N дней чистятся
    sla_min_score: int = 7  # SLA считаем только для лидов с таким баллом и выше
    followup_hours: int = 24  # напомнить, если после «Написал» нет ответа
    spam_chats_per_hour: int = 3  # автор засветился в стольких чатах за час → рассылка, игнорируем
    discover_join_per_day: int = 5  # автопоиск чатов: сколько вступать в день

    # Mini App (кабинет внутри Telegram): публичный HTTPS-адрес, где доступен этот сервер
    miniapp_url: str | None = None
    miniapp_port: int = 8080
    miniapp_tunnel: str = ""  # "cloudflare" — поднять бесплатный quick tunnel и не думать о домене

    # Telegram-аккаунт (Telethon) для чтения чатов. Бот сам в чужие чаты не попадёт.
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "data/userbot"
    tg_chats: StrList = Field(default_factory=list)  # пусто = все группы аккаунта
    tg_join_chats: StrList = Field(default_factory=list)  # @username или ссылки-приглашения, вступить при старте
    tg_search_interval: int = 600
    tg_search_words: StrList = Field(
        default_factory=lambda: ["сайт", "лендинг", "бот", "автоматизация", "нейросеть", "мультик", "анимация"]
    )

    # Threads: официальный Keyword Search API (разрешение threads_keyword_search)
    threads_access_token: str | None = None
    threads_interval: int = 900

    # Instagram Graph API: поиск по хэштегам (бизнес-аккаунт + Facebook-приложение)
    ig_access_token: str | None = None
    ig_user_id: str | None = None
    ig_hashtags: StrList = Field(
        default_factory=lambda: ["нуженсайт", "ищуразработчика", "нуженбот", "нужнаавтоматизация", "заказатьсайт", "ищуподрядчика"]
    )
    ig_interval: int = 1800

    # Яндекс.Карты: бизнесы без сайта (Geosearch API, ключ бесплатный)
    yandex_geosearch_key: str | None = None
    yandex_cities: StrList = Field(default_factory=lambda: ["Москва"])
    yandex_queries: StrList = Field(
        default_factory=lambda: ["салон красоты", "стоматология", "автосервис", "кофейня", "фитнес клуб", "ремонт квартир"]
    )
    yandex_interval: int = 6 * 3600

    # Биржи с публичным RSS заказов
    rss_feeds: StrList = Field(default_factory=lambda: ["https://www.fl.ru/rss/all.xml"])
    rss_interval: int = 300

    # hh.ru — проектные вакансии. Публичный API с 2026 отдаёт 403 неавторизованным клиентам,
    # поэтому по умолчанию выключен (код источника оставлен)
    hh_enabled: bool = False
    hh_user_agent: str = "LeadHunter/0.1 (contact@example.com)"
    hh_queries: StrList = Field(
        default_factory=lambda: ["разработка сайта", "telegram бот", "автоматизация нейросеть", "ИИ анимация"]
    )
    hh_interval: int = 900

    # VK: newsfeed.search по постам (нужен user-токен)
    vk_token: str | None = None
    vk_interval: int = 300

    # Поиск по Threads/Instagram/форумам через поисковики (Serper — платный ключ, иначе DuckDuckGo)
    serper_api_key: str | None = None
    web_search_sites: StrList = Field(default_factory=lambda: ["threads.net", "instagram.com", "vc.ru", "pikabu.ru"])
    web_search_interval: int = 1800

    # ИИ-фильтр (любой OpenAI-совместимый API). Без ключа работает словарный скоринг.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    llm_min_score: int = 2  # словарный балл, с которого текст отправляется ИИ (2 = хотя бы одно слово по теме)
    llm_max_per_hour: int = 300

    # Ежедневный дайджест
    digest_hour: int | None = 9
    digest_tz: str = "Europe/Moscow"

    # === Контент-фабрика ===
    content_dir: str = "data/content"
    content_hour: int = 10  # автопилот приносит черновик в этот час
    # Gemini: один ключ (aistudio.google.com, есть бесплатный лимит) — тексты, картинки, озвучка и ИИ-видео (Veo).
    # Если OPENAI_API_KEY не задан, тексты тоже идут в Gemini через OpenAI-совместимый endpoint.
    gemini_api_key: str | None = None
    gemini_model: str = ""  # текстовая модель Gemini; пусто = значение по умолчанию из content/gemini.py
    gemini_image_model: str = ""
    gemini_video_model: str = ""
    gemini_tts_model: str = ""
    gemini_tts_voice: str = "Kore"
    # OpenAI-медиа (если нет Gemini): картинки и озвучка
    image_model: str = ""  # gpt-image-1 / dall-e-3; пусто = обложки по шаблону (без ИИ)
    tts_model: str = ""  # tts-1 / gpt-4o-mini-tts; пусто = видео без озвучки
    tts_voice: str = "alloy"
    threads_user_id: str | None = None  # публикация в Threads (вместе с THREADS_ACCESS_TOKEN)
    yt_client_id: str | None = None  # YouTube Shorts: OAuth-приложение + refresh token
    yt_client_secret: str | None = None
    yt_refresh_token: str | None = None

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _split_ints(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.split(",") if x.strip()]
        return v

    @field_validator(
        "tg_chats", "tg_join_chats", "tg_search_words", "ig_hashtags", "yandex_cities", "yandex_queries",
        "rss_feeds", "hh_queries", "web_search_sites", "search_phrases", "kwork_categories", "weblancer_urls",
        "freelance_ru_queries", mode="before",
    )
    @classmethod
    def _split_strs(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

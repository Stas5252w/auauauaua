import re
from dataclasses import dataclass, field

# Основы слов по услугам: "сайт" ловит "сайта", "сайты", "сайтик"
TOPIC_CATEGORIES: dict[str, list[str]] = {
    "сайт": ["сайт", "лендинг", "landing", "интернет-магазин", "интернет магазин", "веб", "web", "тильд", "tilda",
             "wordpress", "битрикс", "bitrix", "квиз", "визитк", "дизайн сайта"],
    "бот": ["бот", "chatbot", "чат-бот", "телеграм-бот", "telegram-бот", "mini app", "miniapp",
            "мини-приложение", "миниапп"],
    "автоматизация": ["автоматизац", "нейросет", "нейронк", "ии", "ai", "chatgpt", "gpt", "воронк", "crm",
                      "amocrm", "амо", "интеграц", "парсер", "парсинг", "n8n", "ии-агент", "ai-агент",
                      "1с", "1c"],
    "мультик": ["мультик", "мультфильм", "анимац", "аватар", "ролик", "видеоролик", "рилс", "reels", "sora", "veo"],
    "другое": ["приложение", "логотип"],
}
DEFAULT_TOPIC: dict[str, str] = {w: cat for cat, words in TOPIC_CATEGORIES.items() for w in words}
# Слабые темы (дизайн отдельно от разработки) весят 1 балл вместо 2
LOW_WEIGHT_TOPICS = {"дизайн сайта", "логотип"}
DEFAULT_INTENT = [
    "ищу", "ищем", "нужен", "нужна", "нужно", "нужны", "требуется", "кто сделает", "кто может сделать",
    "кто занимается", "кто возьмется", "посоветуйте", "порекомендуйте", "подскажите", "подскажите кто",
    "сколько стоит", "сколько будет стоить", "заказать", "хочу заказать", "подрядчик", "бюджет", "есть тз",
    "срочно", "срочно нужно", "нужен человек", "готов платить", "оплачу",
    "ищу исполнител", "ищу подрядчик", "ищу специалист", "ищу программист", "ищу разработчик", "ищу фрилансер",
]
# Чужие офферы, резюме и реклама — это не клиенты. «курс»/«обучение» сами по себе не штрафуем:
# заказчик тоже пишет «сайт для онлайн-курса» — только фразы продавца.
DEFAULT_NEGATIVE = [
    "сделаю", "разработаю", "создам", "настрою", "предлагаю услуг", "оказываю услуг", "наша студия",
    "наша команда", "мы делаем", "делаю сайт", "делаю бот", "занимаюсь разработкой", "помогу с", "резюме",
    "ищу работу", "ищу заказы", "ищу клиентов", "портфолио", "возьму заказ", "пишите в лс", "пишите в личку",
    "сайты под ключ", "боты под ключ", "лендинги под ключ",
    "курс по", "курсы по", "обучу", "обучаю", "научу", "записывайся", "запись на курс", "продаю курс",
    "приглашаем на вебинар", "приглашаю на вебинар", "бесплатный вебинар",
    "скидк", "акция", "рассылк", "кейс:", "услуги по",
]


@dataclass
class Keywords:
    topic: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TOPIC))  # основа -> услуга
    intent: list[str] = field(default_factory=lambda: list(DEFAULT_INTENT))
    negative: list[str] = field(default_factory=lambda: list(DEFAULT_NEGATIVE))


@dataclass
class ScoreResult:
    score: int
    topic: list[str] = field(default_factory=list)
    intent: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)

    @property
    def topic_hits(self) -> int:
        return len(self.topic)

    @property
    def intent_hits(self) -> int:
        return len(self.intent)

    @property
    def strong_intent(self) -> bool:
        """Есть фраза-намерение из нескольких слов («ищу исполнителя», «кто возьмётся») — не просто «ищу»/«нужно»."""
        return any(" " in i for i in self.intent)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


# Окончания для коротких основ: «бот» → «боты», «ботов», но не «ботинки»/«работа»; «веб» не ловит «вебинар»
_ENDINGS = r"(?:а|у|ы|е|и|ом|ов|ам|ах|ами|ой|ою)?"


def _pattern(stem: str) -> re.Pattern:
    stem = normalize(stem)
    if len(stem) <= 2:
        tail = r"\b"  # «ии», «ai» — только целое слово
    elif len(stem) <= 4:
        tail = _ENDINGS + r"(?!\w)"
    else:
        tail = r"\w*"
    return re.compile(r"(?<!\w)" + re.escape(stem) + tail)


def find_matches(text: str, stems) -> list[str]:
    """Основы, найденные в тексте. Совпадение внутри более длинного («бот» в «чат-бот») считается один раз."""
    found: list[tuple[str, int, int]] = []
    for s in stems:
        m = _pattern(s).search(text)
        if m:
            found.append((s, m.start(), m.end()))
    return [s for s, a, b in found
            if not any((a2, b2) != (a, b) and a2 <= a and b <= b2 for _, a2, b2 in found)]


def score_text(text: str, kw: Keywords, intent_implied: bool = False) -> ScoreResult:
    norm = normalize(text)
    topic = find_matches(norm, kw.topic)
    intent = find_matches(norm, kw.intent)
    negative = find_matches(norm, kw.negative)
    services = list(dict.fromkeys(kw.topic.get(t, "другое") for t in topic))
    if not topic:
        return ScoreResult(0, topic, intent, negative, services)
    intent_hits = max(len(intent), 1 if intent_implied else 0)
    topic_points = min(sum(1 if t in LOW_WEIGHT_TOPICS else 2 for t in topic), 6)
    score = topic_points + min(intent_hits, 2) * 3 - len(negative) * 3
    return ScoreResult(max(0, min(score, 10)), topic, intent, negative, services)

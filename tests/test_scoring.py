import pytest

from leadhunter.scoring import Keywords, score_text


def test_request_with_intent_scores_high():
    r = score_text("Ребята, ищу кто сделает сайт под ключ, бюджет 50к", Keywords())
    assert r.score >= 5
    assert "сайт" in r.topic and "ищу" in r.intent
    assert r.services == ["сайт"]


def test_competitor_offer_is_penalized():
    r = score_text("Сделаю сайт быстро и дёшево, портфолио в профиле, пишите в лс", Keywords())
    assert r.score < 5
    assert "сделаю" in r.negative


def test_offtopic_scores_zero():
    assert score_text("Кто знает хороший ресторан в центре?", Keywords()).score == 0


def test_short_stems_match_whole_words_only():
    assert score_text("нужен ии ассистент", Keywords()).topic == ["ии"]
    assert "ии" not in score_text("нужен иисус", Keywords()).topic


def test_yo_normalization_and_stems():
    r = score_text("Нужён лендинг для кофейни", Keywords())
    assert "лендинг" in r.topic and "нужен" in r.intent


def test_intent_implied_for_boards():
    text = "Разработка сайта для стоматологии"
    assert score_text(text, Keywords()).score == 2
    assert score_text(text, Keywords(), intent_implied=True).score == 5


def test_services_detected_in_order():
    r = score_text("Нужен бот и мультик для детского сада, автоматизация записи", Keywords())
    assert r.services == ["бот", "автоматизация", "мультик"]


def test_no_substring_false_positives():
    for text in ("Нужны ботинки на зиму, срочно", "Нужна работа, заботы одолели", "Приглашаю на вебинар, нужно записаться",
                 "Нужна амортизация склада"):
        assert score_text(text, Keywords()).topic == [], text
    assert score_text("Нужны боты для магазина", Keywords()).topic == ["бот"]
    assert "сайт" in score_text("Нужен веб-сайт", Keywords()).topic


def test_compound_words_count_once():
    assert score_text("нужен чат-бот", Keywords()).topic == ["чат-бот"]
    assert score_text("нужен ai-агент для продаж", Keywords()).topic == ["ai-агент"]
    assert score_text("нужен телеграм-бот", Keywords()).topic == ["телеграм-бот"]


def test_course_is_negative_only_for_sellers():
    assert score_text("Нужен сайт для онлайн-курса, бюджет 50к", Keywords()).score >= 5
    assert score_text("Нужен чат-бот для обучения сотрудников", Keywords()).score >= 5
    assert score_text("Нужен лендинг для курса по йоге, бюджет 30к", Keywords()).score >= 5
    assert "курс по" in score_text("Курс по созданию ботов, записывайся! Бот за выходные", Keywords()).negative


# (текст, ожидаем лид при пороге 5?)
REALISTIC = [
    ("Ищу исполнителя: нужно сделать приложение под iOS", True),
    ("Требуется программист 1С для доработки конфигурации", True),
    ("Кто возьмётся сделать парсер маркетплейса? Бюджет 30к", True),
    ("Подскажите кто делает интернет-магазин на Tilda, сколько стоит?", True),
    ("Хочу заказать мини-приложение в телеграм, есть ТЗ", True),
    ("Срочно нужен чат-бот для записи клиентов, бюджет 20 000 руб", True),
    ("Нужна интеграция CRM с сайтом, кто может сделать?", True),
    ("Нужен дизайн сайта и логотип, бюджет 15к", True),
    ("Делаю сайты на Tilda, портфолио в профиле, пишите в лс", False),
    ("Занимаюсь разработкой ботов и приложений. Возьму заказ, оказываю услуги под ключ", False),
    ("Помогу с настройкой CRM и интеграцией, ищу клиентов", False),
    ("Курс по созданию чат-ботов, записывайся! Скидка 50%", False),
    ("Ищу работу: разработчик сайтов, резюме в лс", False),
    ("Нужны ботинки на зиму, посоветуйте магазин", False),
    ("Нужен человек на склад, заботы о доставке", False),
]


@pytest.mark.parametrize("text,is_lead", REALISTIC)
def test_realistic_messages(text, is_lead):
    r = score_text(text, Keywords())
    assert (r.score >= 5) is is_lead, (r.score, r.topic, r.intent, r.negative)


def test_strong_intent_without_topic():
    r = score_text("Ищу исполнителя для небольшой задачи, детали в лс", Keywords())
    assert r.score == 0 and r.topic_hits == 0 and r.strong_intent
    assert not score_text("Ищу квартиру недорого", Keywords()).strong_intent

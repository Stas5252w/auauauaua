"""Публичная страница лид-магнита: содержание + форма «получить». Заявка уходит в пайплайн как горячий лид."""
import html


def _page(title: str, brand, body: str) -> str:
    e = html.escape
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<style>
:root{{--bg:{e(brand.primary)};--ac:{e(brand.accent)};--fg:{e(brand.text)}}}
*{{box-sizing:border-box}}body{{margin:0;font:17px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto;padding:40px 20px 80px}}h1{{font-size:34px;line-height:1.15;margin:0 0 12px}}
.sub{{opacity:.8;font-size:19px;margin-bottom:32px}}.bar{{width:64px;height:6px;background:var(--ac);border-radius:3px;margin-bottom:24px}}
ol{{padding-left:0;list-style:none;counter-reset:n}}li{{counter-increment:n;margin:0 0 18px;padding-left:46px;position:relative}}
li::before{{content:counter(n);position:absolute;left:0;top:2px;width:32px;height:32px;border-radius:50%;background:var(--ac);color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:15px}}
li b{{display:block;margin-bottom:2px}}li span{{opacity:.85}}
form{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:22px;margin-top:36px}}
label{{display:block;font-size:14px;opacity:.8;margin:12px 0 4px}}input,textarea{{width:100%;padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.25);color:var(--fg);font:inherit}}
button{{margin-top:18px;width:100%;padding:14px;border:0;border-radius:12px;background:var(--ac);color:#fff;font:600 17px inherit;cursor:pointer}}
.hp{{position:absolute;left:-9999px}}footer{{opacity:.5;font-size:13px;margin-top:40px;text-align:center}}
</style></head><body><main>{body}
<footer>{e(brand.name)} {e(brand.handle)}</footer></main></body></html>"""


def render_landing(row, data: dict, brand) -> str:
    e = html.escape
    title = data.get("title") or row["title"] or row["topic"]
    items = "".join(f"<li><b>{e(i.get('title', ''))}</b><span>{e(i.get('text', ''))}</span></li>" for i in data.get("items") or [])
    cta = data.get("cta") or "Оставьте контакт — пришлём материал и разберём вашу задачу."
    body = f"""<div class="bar"></div><h1>{e(title)}</h1><p class="sub">{e(data.get('subtitle', ''))}</p>
<ol>{items}</ol>
<form method="post"><p style="margin:0 0 6px;font-weight:600">{e(cta)}</p>
<label>Как вас зовут</label><input name="name" maxlength="100">
<label>Телефон или Telegram</label><input name="contact" required maxlength="100" placeholder="@username или +7…">
<label>Что хотите сделать (необязательно)</label><textarea name="message" rows="3" maxlength="1000"></textarea>
<input class="hp" name="website" tabindex="-1" autocomplete="off">
<button type="submit">Получить</button></form>"""
    return _page(title, brand, body)


def render_thanks(row, brand) -> str:
    e = html.escape
    body = (f'<div class="bar"></div><h1>Спасибо!</h1><p class="sub">Заявка по «{e(row["title"] or row["topic"])}» получена — '
            f'напишем вам в ближайшее время.</p>')
    return _page("Спасибо", brand, body)


def render_notice(title: str, text: str, brand) -> str:
    e = html.escape
    return _page(title, brand, f'<div class="bar"></div><h1>{e(title)}</h1><p class="sub">{e(text)}</p>')

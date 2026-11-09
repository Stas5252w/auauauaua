#!/usr/bin/env bash
# Установщик LeadHunter «для чайников»: спросит токен бота, поставит всё нужное и запустит.
# Запуск:  curl -fsSL https://raw.githubusercontent.com/Stas5252/aiiiiiiiiiiiiiiiiiii/main/install.sh | bash
# Для автоматики (без вопросов): BOT_TOKEN=... [GEMINI_API_KEY=...] LEADHUNTER_YES=1 ./install.sh
set -euo pipefail

REPO_URL="https://github.com/Stas5252/aiiiiiiiiiiiiiiiiiii.git"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
YES="${LEADHUNTER_YES:-}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

# Ввод идёт из терминала, даже когда скрипт запущен через `curl | bash`.
tty_read() { # tty_read <silent:0|1> <prompt> -> REPLY
    local silent="$1" prompt="$2"
    REPLY=""
    if [ -n "$YES" ] || [ ! -r /dev/tty ]; then return 0; fi
    if [ "$silent" = 1 ]; then read -rs -p "$prompt" REPLY </dev/tty || true; echo
    else read -r -p "$prompt" REPLY </dev/tty || true; fi
}

confirm() { # confirm <вопрос> [по умолчанию y|n]
    local def="${2:-y}"
    if [ -n "$YES" ]; then [ "$def" = y ]; return; fi
    tty_read 0 "$1 [$([ "$def" = y ] && echo Д/н || echo д/Н)] "
    case "${REPLY,,}" in
        д|да|y|yes) return 0 ;;
        н|нет|n|no) return 1 ;;
        *) [ "$def" = y ] ;;
    esac
}

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

OS="other"
case "$(uname -s)" in
    Darwin) OS="mac" ;;
    Linux)  if [ -r /etc/os-release ] && grep -qiE 'ubuntu|debian' /etc/os-release; then OS="debian"; else OS="linux"; fi ;;
esac

# ---------- где лежит проект ----------
if [ -z "$SELF_DIR" ] || [ ! -f "$SELF_DIR/pyproject.toml" ]; then
    say "Скачиваю LeadHunter"
    if ! command -v git >/dev/null 2>&1; then
        [ "$OS" = debian ] || die "Установите git и запустите снова."
        confirm "Нужен git. Установить через apt?" y || die "Без git не получится."
        $SUDO apt-get update -y && $SUDO apt-get install -y git
    fi
    TARGET="${LEADHUNTER_DIR:-$HOME/leadhunter}"
    if [ -d "$TARGET/.git" ]; then git -C "$TARGET" pull --ff-only || warn "Не удалось обновить, использую то, что есть."
    else git clone --depth 1 "$REPO_URL" "$TARGET"; fi
    exec bash "$TARGET/install.sh" "$@"
fi
cd "$SELF_DIR"

# ---------- .env ----------
# Значение передаётся через окружение awk: не попадает ни в вывод, ни в аргументы процессов.
set_env() { # set_env KEY VALUE
    local tmp; tmp="$(mktemp)"
    V="$2" awk -v k="$1" 'BEGIN{done=0} index($0,k"=")==1 && !done {print k"="ENVIRON["V"]; done=1; next} {print} END{if(!done) print k"="ENVIRON["V"]}' .env > "$tmp"
    cat "$tmp" > .env; rm -f "$tmp"
}
get_env() { { grep -E "^$1=" .env 2>/dev/null || true; } | head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//'; }

valid_token() { [[ "$1" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; }

setup_env() {
    say "Настройка бота"
    [ -f .env ] || cp .env.example .env
    chmod 600 .env 2>/dev/null || true

    local token="${BOT_TOKEN:-}" current; current="$(get_env BOT_TOKEN)"
    if [ -z "$token" ] && valid_token "$current"; then
        if confirm "Токен бота в .env уже есть. Оставить его?" y; then token="$current"; fi
    fi
    if [ -z "$token" ]; then
        echo "Токен выдаёт @BotFather в Telegram: напишите ему /newbot, придумайте имя — он пришлёт строку вида 123456:ABC…"
        echo "(ввод скрыт — просто вставьте токен и нажмите Enter)"
        while :; do
            tty_read 1 "Токен бота: "; token="${REPLY// /}"
            valid_token "$token" && break
            if [ -n "$YES" ] || [ ! -r /dev/tty ]; then die "Токен не задан или неверного формата. Передайте BOT_TOKEN=..."; fi
            warn "Это не похоже на токен (нужно 123456:AAA…). Попробуйте ещё раз."
        done
    fi
    valid_token "$token" || die "BOT_TOKEN неверного формата."
    set_env BOT_TOKEN "$token"

    if command -v curl >/dev/null 2>&1 && [ -z "${LEADHUNTER_SKIP_NET:-}" ]; then
        local name=""
        # URL с токеном идёт через stdin (--config -), чтобы он не светился в `ps`
        name="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$token" | curl -fsS --max-time 10 --config - 2>/dev/null | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')" || true
        if [ -n "$name" ]; then ok "Токен принят: @$name"; else warn "Не смог проверить токен (нет сети или токен неверный) — продолжаю."; fi
    fi

    local gem="${GEMINI_API_KEY:-}"
    if [ -z "$gem" ] && [ -z "$(get_env GEMINI_API_KEY)" ]; then
        echo
        echo "Ключ Gemini (ИИ для текстов и озвучки) — бесплатно: aistudio.google.com → Get API key."
        echo "Можно пропустить (Enter) и добавить позже в боте командой /setup."
        tty_read 1 "Ключ Gemini: "; gem="${REPLY// /}"
    fi
    if [ -n "$gem" ]; then set_env GEMINI_API_KEY "$gem"; fi

    local owner="${OWNER_IDS:-}"
    if [ -z "$owner" ]; then owner="$(get_env OWNER_IDS)"; fi
    if [ -z "$owner" ]; then
        echo
        echo "Ваш числовой Telegram ID — напишите @userinfobot, он ответит числом. Это защита: только вы сможете управлять ботом."
        echo "Несколько владельцев — через запятую. Можно пропустить (Enter)."
        tty_read 0 "Ваш Telegram ID: "; owner="${REPLY// /}"
    fi
    if [ -n "$owner" ] && ! printf '%s' "$owner" | grep -qE '^[0-9]+(,[0-9]+)*$'; then
        warn "ID должен состоять из цифр (через запятую) — пропускаю."; owner=""
    fi
    if [ -n "$owner" ]; then
        set_env OWNER_IDS "$owner"
    else
        warn "OWNER_IDS не задан. Бот сгенерирует одноразовый код привязки и напишет его в логи сервера —"
        warn "его нужно будет отправить боту: /start КОД (docker compose logs / journalctl)."
    fi

    set_env MINIAPP_TUNNEL "cloudflare"
    ok ".env готов (права 600, ключи не показываются)"
}

# ---------- Docker ----------
DOCKER=""
docker_ready() {
    command -v docker >/dev/null 2>&1 || return 1
    if docker info >/dev/null 2>&1; then DOCKER="docker"
    elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"
    else return 1; fi
    $DOCKER compose version >/dev/null 2>&1
}

run_docker() {
    say "Запускаю в Docker (первая сборка занимает 3–10 минут)"
    $DOCKER compose up -d --build
    ok "Контейнер запущен"
    echo "  Логи: $DOCKER compose logs -f    Остановить: $DOCKER compose down"
}

# ---------- Python-режим ----------
python_ok() { command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

find_python() {
    local p
    for p in python3.13 python3.12 python3.11 python3; do
        if python_ok "$p"; then echo "$p"; return 0; fi
    done
    return 1
}

run_python() {
    say "Устанавливаю без Docker (Python)"
    local py=""
    py="$(find_python)" || true
    if [ -z "$py" ]; then
        case "$OS" in
            debian) confirm "Нужен Python 3.11+. Установить через apt?" y || die "Без Python не получится."
                    $SUDO apt-get update -y && $SUDO apt-get install -y python3 python3-venv python3-pip ;;
            mac)    command -v brew >/dev/null 2>&1 || die "Нужен Homebrew (https://brew.sh) или Docker Desktop (https://docker.com/products/docker-desktop)."
                    confirm "Нужен Python 3.12. Установить через brew?" y || die "Без Python не получится."
                    brew install python@3.12 ;;
            *)      die "Установите Python 3.11+ и запустите снова." ;;
        esac
        py="$(find_python)" || die "Нужен Python 3.11 или новее (на вашей системе он старый). Проще поставить Docker и запустить установщик снова."
    fi
    ok "Python: $("$py" --version)"

    if ! command -v ffmpeg >/dev/null 2>&1 || { [ "$OS" = debian ] && ! dpkg -s fonts-dejavu-core >/dev/null 2>&1; }; then
        case "$OS" in
            debian) if confirm "Для видео и картинок нужны ffmpeg и шрифты. Установить через apt?" y; then
                        $SUDO apt-get update -y && $SUDO apt-get install -y ffmpeg fonts-dejavu-core python3-venv curl ca-certificates
                    else warn "Без ffmpeg видео и мультики работать не будут."; fi ;;
            mac)    if command -v brew >/dev/null 2>&1 && confirm "Для видео нужен ffmpeg. Установить через brew?" y; then brew install ffmpeg
                    else warn "Без ffmpeg видео и мультики работать не будут."; fi ;;
            *)      warn "Поставьте ffmpeg вручную — он нужен для видео." ;;
        esac
    fi

    [ -d .venv ] || "$py" -m venv .venv || die "Не удалось создать .venv (на Ubuntu: sudo apt install python3-venv)."
    .venv/bin/pip install -q --upgrade pip
    say "Ставлю зависимости (1–3 минуты)"
    .venv/bin/pip install -q .
    ok "Зависимости установлены"
    if confirm "Установить браузер для парсинга сайтов (≈300 МБ, нужен для части источников)?" n; then
        .venv/bin/patchright install chromium || warn "Браузер не поставился — источники на нём будут пропущены."
    fi

    install_cloudflared
    mkdir -p data
    write_service
}

install_cloudflared() {
    if command -v cloudflared >/dev/null 2>&1; then ok "cloudflared уже есть"; return; fi
    if [ -x bin/cloudflared ]; then ok "cloudflared уже в ./bin"; return; fi
    say "Ставлю cloudflared (бесплатный HTTPS-адрес для кабинета)"
    if [ "$OS" = mac ]; then
        if command -v brew >/dev/null 2>&1; then brew install cloudflared || warn "Не вышло — кабинет будет выключен."
        else warn "Поставьте cloudflared (brew install cloudflared) — без него кабинет не откроется."; fi
        return
    fi
    local arch; arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) warn "Архитектура $arch: поставьте cloudflared вручную — без него кабинет не откроется."; return ;;
    esac
    mkdir -p bin
    if curl -fsSL -o bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"; then
        chmod +x bin/cloudflared; ok "cloudflared скачан в ./bin"
    else rm -f bin/cloudflared; warn "Не удалось скачать cloudflared — кабинет (Mini App) будет выключен."; fi
}

RUN_CMD=""
write_service() {
    RUN_CMD="cd '$SELF_DIR' && PATH='$SELF_DIR/bin':\$PATH .venv/bin/python -m leadhunter"
    local can_root=0
    if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then can_root=1; fi
    if [ "$OS" = debian ] && [ "$can_root" = 1 ] && command -v systemctl >/dev/null 2>&1 \
        && confirm "Запускать бота автоматически при старте сервера (systemd)?" y; then
        local user; user="$(id -un)"
        $SUDO tee /etc/systemd/system/leadhunter.service >/dev/null <<UNIT
[Unit]
Description=LeadHunter
After=network-online.target
Wants=network-online.target

[Service]
User=$user
WorkingDirectory=$SELF_DIR
Environment=PATH=$SELF_DIR/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$SELF_DIR/.venv/bin/python -m leadhunter
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable --now leadhunter
        ok "Служба запущена. Логи: journalctl -u leadhunter -f"
        RUN_CMD=""
    fi
}

# ---------- выбор режима ----------
say "LeadHunter — установка"
setup_env

if [ -n "${LEADHUNTER_ENV_ONLY:-}" ]; then ok "Только .env (LEADHUNTER_ENV_ONLY)"; exit 0; fi

if docker_ready; then
    ok "Docker найден"
    run_docker
else
    if command -v docker >/dev/null 2>&1; then
        warn "Docker установлен, но не отвечает (не запущен или нет прав)."
        if [ "$OS" = mac ]; then warn "Откройте приложение Docker Desktop и запустите установщик снова."; fi
    elif [ "$OS" = debian ] && confirm "Docker не найден. Установить его официальным скриптом get.docker.com? (проще всего)" y; then
        curl -fsSL https://get.docker.com | $SUDO sh
        if [ -n "$SUDO" ]; then $SUDO usermod -aG docker "$(id -un)" 2>/dev/null || true; fi
    elif [ "$OS" = mac ]; then
        echo "Для Mac проще всего Docker Desktop: https://docker.com/products/docker-desktop"
    fi
    if docker_ready; then
        run_docker
    else
        confirm "Запустить без Docker (через Python)?" y || die "Установите Docker и запустите установщик снова."
        run_python
    fi
fi

say "Готово: напишите боту /start"
if [ -z "$(get_env OWNER_IDS)" ]; then
    echo "  Владелец не задан: возьмите код из логов бота («Владелец не задан…») и отправьте боту: /start КОД"
fi
echo "  Дальше в боте: выберите нишу кнопкой → /setup (ключ ИИ) → кнопка «Кабинет»."
if [ -n "$RUN_CMD" ]; then
    echo
    echo "  Запуск бота:  $RUN_CMD"
fi
echo "  Обновление:   ./update.sh"

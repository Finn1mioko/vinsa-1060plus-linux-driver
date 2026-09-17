#!/usr/bin/env bash
set -euo pipefail

APP_NAME="vinsa-t501"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
BIN_PATH="/usr/local/bin/${APP_NAME}"
SERVICE_NAME="${APP_NAME}.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
UDEV_PATH="/etc/udev/rules.d/70-${APP_NAME}.rules"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Ошибка: установщик нужно запускать от root."
    echo "Используйте: sudo ./install.sh"
    exit 1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Ошибка: этот драйвер предназначен для Linux."
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Ошибка: systemd не найден."
    echo "Эта версия установщика рассчитана на систему с systemd."
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/driver.py" ]]; then
    echo "Ошибка: рядом с install.sh не найден driver.py"
    exit 1
fi

echo "==> VINSA T501 driver installer"
echo

# ------------------------------------------------------------
# Установка системных зависимостей на Debian/Ubuntu
# ------------------------------------------------------------

if command -v apt-get >/dev/null 2>&1; then
    echo "==> Обновление списка пакетов..."
    apt-get update

    echo "==> Установка системных зависимостей..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 \
        python3-venv \
        python3-dev \
        build-essential \
        libusb-1.0-0 \
        libusb-1.0-0-dev \
        udev
else
    echo "==> apt-get не найден."
    echo "    Предполагается, что Python 3, venv, компилятор и libusb уже установлены."
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Ошибка: python3 не найден."
    exit 1
fi

# ------------------------------------------------------------
# Остановка старой версии
# ------------------------------------------------------------

echo "==> Остановка ранее установленного сервиса..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

# ------------------------------------------------------------
# Создание каталога
# ------------------------------------------------------------

echo "==> Установка файлов в ${APP_DIR}..."
install -d -m 0755 "${APP_DIR}"

install -m 0644 \
    "${SCRIPT_DIR}/driver.py" \
    "${APP_DIR}/driver.py"

# ------------------------------------------------------------
# Создание virtualenv
# ------------------------------------------------------------

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "==> Создание Python virtualenv..."
    python3 -m venv "${VENV_DIR}"
fi

PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

echo "==> Обновление pip..."
"${PYTHON}" -m pip install --upgrade pip

echo "==> Установка Python-зависимостей..."
"${PIP}" install --upgrade \
    pyusb \
    evdev

# ------------------------------------------------------------
# uinput
# ------------------------------------------------------------

echo "==> Проверка uinput..."

if ! modprobe uinput 2>/dev/null; then
    echo "Предупреждение: не удалось загрузить модуль uinput."
    echo "Попробуйте вручную: sudo modprobe uinput"
fi

# ------------------------------------------------------------
# Установка launcher
# ------------------------------------------------------------

echo "==> Установка команды ${APP_NAME}..."

cat > "${BIN_PATH}" <<EOF
#!/usr/bin/env bash
exec "${PYTHON}" "${APP_DIR}/driver.py" "\$@"
EOF

chmod 0755 "${BIN_PATH}"

# ------------------------------------------------------------
# Установка udev rule
# ------------------------------------------------------------

if [[ -f "${SCRIPT_DIR}/70-${APP_NAME}.rules" ]]; then
    echo "==> Установка udev rule..."
    install -m 0644 \
        "${SCRIPT_DIR}/70-${APP_NAME}.rules" \
        "${UDEV_PATH}"

    udevadm control --reload-rules
    udevadm trigger || true
fi

# ------------------------------------------------------------
# Установка systemd service
# ------------------------------------------------------------

echo "==> Установка systemd service..."

install -m 0644 \
    "${SCRIPT_DIR}/${SERVICE_NAME}" \
    "${SERVICE_PATH}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# ------------------------------------------------------------
# Запуск
# ------------------------------------------------------------

echo "==> Запуск драйвера..."
systemctl restart "${SERVICE_NAME}"

echo
echo "=========================================="
echo " VINSA T501 driver установлен."
echo "=========================================="
echo
echo "Статус:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo
echo "Логи:"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
echo
echo "Ручной запуск:"
echo "  sudo ${BIN_PATH} --debug"
echo
echo "Установка завершена."

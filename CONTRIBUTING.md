# Как внести изменения

1. Запусти `./scripts/setup.sh` с Python 3.11 или новее.
2. Проверь изменения командой `.venv/bin/python -m unittest discover -s tests -v`.
   Тесты используют подставные ответы, поэтому профиль и Telegram не нужны.
3. В pull request опиши, какой сценарий меняется и как его проверить. Если
   меняется команда или параметр, обнови README и пример конфигурации.

Для воспроизведения ошибки используй вымышленные данные. При добавлении
сетевых сценариев сохраняй последовательный опрос и ограничение частоты.

## Запуск и сборка настольного приложения

Для запуска окна из исходников установи зависимости из
`requirements-desktop.txt` и запусти `python desktop_app.py`.
Интерфейс использует PySide6 и Qt Quick.

Для архива установи зависимости сборки и запусти упаковку на нужной ОС.

```bash
python -m pip install -r requirements-build.txt
python packaging/build_desktop.py
```

GitHub Actions собирает Windows x64 и macOS для Apple Silicon и Intel.
Каждая сборка проверяет, что упакованное приложение может открыть окно и
загрузить комплектный сертификат. Архивы создаются в `dist/`.

Данные настольной версии находятся в `%LOCALAPPDATA%\GorzdravSlotHunter` на
Windows и `~/Library/Application Support/Gorzdrav Slot Hunter` на macOS.
Для изолированных прогонов можно указать другой каталог через
`GORZDRAV_HOME`.

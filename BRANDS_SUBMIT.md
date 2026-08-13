# Логотип интеграции в интерфейсе Home Assistant

Home Assistant берёт иконку на карточке интеграции **только** из официального
репозитория `home-assistant/brands` (через CDN brands.home-assistant.io).
Файл `icon.png` внутри `custom_components/` на карточку не попадает.

Готовые файлы лежат в `custom_components/bms_smart_ir/`:
- `icon.png` — 256×256
- `icon@2x.png` — 512×512

## Порядок действий

1. Форк `home-assistant/brands` уже есть: `optomtr/brands`.
2. Создать в нём папку `custom_integrations/bms_smart_ir/` и положить туда обе
   иконки из этого репозитория.
3. Commit + push, затем открыть Pull Request в `home-assistant/brands`.
4. После принятия PR (обычно несколько дней) вместо «icon not available»
   появится логотип. Home Assistant после этого перезапустить / почистить кеш.

Это только внешний вид — без иконки интеграция работает полностью.

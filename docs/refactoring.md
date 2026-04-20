# План рефакторинга: tg_parser

Документ описывает текущие проблемы кодовой базы и поэтапный план их устранения.

---

## Текущие проблемы

### Критические (баги влияющие на корректность)

| # | Файл | Строка | Проблема | Статус |
|---|---|---|---|---|
| 1 | `web_server.py` | 179 | `open_fee` вычитается дважды при расчёте TP PnL | ✅ Исправлено |
| 2 | `database.py` | 162 | SQL f-string с именами колонок — потенциальная SQL-инъекция | ✅ Исправлено |
| 3 | `database.py` | 82–107 | `_upgrade_db()` без версионирования — риск дублирования колонок | ✅ Исправлено |

### Высокий приоритет (архитектура и надёжность)

**Magic numbers** — захардкоженные значения разбросаны по файлам:
- `bybit_exchange.py:195` — `asyncio.sleep(1.2)` (задержка после ордера, необъяснённая)
- `bybit_exchange.py:293` — `asyncio.sleep(2.0)` (интервал монитора)
- `bybit_exchange.py:370` — `next_step < 3` (максимум 3 DCA, не вынесен в конфиг)
- `main.py:129` — `host="127.0.0.1", port=8000` (адрес веб-сервера)
- `web_server.py:152` — `LIMIT 50` (лимит сигналов)
- `notifier.py:98` — `asyncio.sleep(2)` (интервал поллинга)

**Архитектурные проблемы:**
- `web_server.py:16–26` — глобальные переменные для DI (`set_context()` изменяет глобалы, нет изоляции)
- `main.py:49` — `asyncio.Queue()` без ограничения размера, нет backpressure
- `database.py` — 4 класса в одном файле (270 строк), нет базового класса, нет транзакций
- Нет типизации: `trade: Any`, `notifier: Any` в `bybit_exchange.py`

**Схема БД:**
- `trades` таблица не нормализована: отдельные колонки для каждого DCA-шага (`dca1_p`, `dca2_p`, `dca3_p`) — ограничивает расширяемость
- Нет индексов на `coin`, `status`, `created_at` — замедляет выборки при росте данных
- Метки времени хранятся как TEXT (ISO строки), а не INTEGER (Unix timestamp)
- Нет ограничений на поле `status` (любое значение принимается)

### Средний приоритет (качество кода)

- `database.py` — повторяющийся код: `row_factory` устанавливается в каждом методе (6+ раз)
- `notifier.py` — все команды в одном методе `process_cmd()` (166 строк); нет реестра команд
- `parser.py` — фрагильные regex-паттерны, частичные русские слова (`упить`, `родать`), нет поддержки вариантов написания
- `web_server.py` — N+1 запросов: `get_trading_trade()` вызывается для каждой позиции в цикле
- `config.py` — нет валидации обязательных параметров (пустые API-ключи не вызывают ошибку)
- `arc/` — мёртвый код (bot.py, exchange.py) занимает место в репозитории
- `utils/` — скрипты не покрыты тестами, дублируют логику основного кода

---

## Предлагаемая структура проекта

Текущая плоская структура затрудняет навигацию по мере роста. Предлагается пакетная организация:

```
tg_parser/
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── bot.py              # TradingBot (из main.py)
│   │   └── config.py           # конфиг
│   ├── exchange/
│   │   └── bybit.py            # BybitExchange
│   ├── parser/
│   │   └── signal_parser.py    # parse_signal()
│   ├── database/
│   │   ├── __init__.py
│   │   ├── base.py             # BaseDatabase (абстрактный класс)
│   │   ├── signals.py          # Database
│   │   ├── trades.py           # TradesDatabase
│   │   ├── settings.py         # SettingsDatabase
│   │   └── coins.py            # CoinsDatabase
│   ├── services/
│   │   ├── notifier.py
│   │   └── logger.py
│   └── web/
│       ├── server.py           # FastAPI app
│       └── templates/
│           └── index.html
├── scripts/                    # переименовать utils/
│   ├── auth_me.py
│   ├── fill_coins.py
│   └── ...
├── tests/                      # новая директория
│   ├── test_parser.py
│   └── test_database.py
├── config/
│   └── .env
├── db/
├── logs/
├── docs/
├── main.py                     # тонкий entry point (запуск app)
└── requirements.txt
```

---

## Поэтапный план

### Tier 1 — Критические исправления ✅ Выполнено

- [x] **Исправить баг TP PnL** в `web_server.py:179` — убрано дублирование `open_fee`
- [x] **Устранить SQL через f-string** в `database.py:162` — заменён на whitelist `{1: ("dca1_p","dca1_a"), ...}`
- [x] **Добавить версионирование миграций БД** — таблица `schema_version`, миграции применяются ровно один раз
- [x] **Обновить `.gitignore`** — добавлены `config/.env`, `arc/*.csv`, `arc/*.txt`
- [x] **Очистить `arc/`** — удалены bot.py, exchange.py, repair_db.py, fix_db.py, close_manual.py, старые сессии и лог; CSV сохранены

### Tier 2 — Высокий приоритет (частично выполнено)

- [x] **Вынести magic numbers в config** — добавлены `WEB_HOST`, `WEB_PORT`, `SIGNAL_QUEUE_MAX`, `MONITOR_INTERVAL`, `EXEC_DATA_DELAY`, `DCA_MAX_STEPS`, `SIGNALS_LIMIT`, `NOTIFIER_POLL_INTERVAL`
- [x] **Ограничить `asyncio.Queue`** — `asyncio.Queue(maxsize=config.SIGNAL_QUEUE_MAX)` в `main.py`
- [x] **Разбить `database.py`** — создан пакет `database/` с модулями `base.py`, `signals.py`, `trades.py`, `settings.py`, `coins.py`
- [x] **Добавить индексы БД**: `idx_trades_coin_status` в trades, `idx_signals_received_at` в signals
- [x] **Заменить глобальные переменные** в `web_server.py` — глобалы заменены на `app.state`

### Tier 3 — Средний приоритет

- [ ] **Пакетная структура `app/`** — переместить модули по папкам (без изменения логики)
- [ ] **Нормализовать схему trades** — вынести DCA-шаги в отдельную таблицу `dca_fills(trade_id, step, price, amount, fee)`
- [ ] **Типизация** — заменить `Any` на конкретные типы/протоколы (`TradeRow`, `ExchangeProtocol`)
- [ ] **Реестр команд** в `notifier.py` — словарь `{"/status": handler_func}` вместо if-elif цепочки
- [ ] **Устранить N+1 запросы** в `web_server.py` — один запрос на все позиции

### Tier 4 — Низкий приоритет (качество и polish)

- [ ] **Пиннинг зависимостей** в `requirements.txt` (добавить версии: `pybit==5.x.x`)
- [ ] **Тесты** в `tests/` для `parser.py` и ключевых методов `database.py`
- [ ] **Валидация конфига** при старте — проверять обязательные поля, выбрасывать понятные ошибки
- [ ] **Улучшить `parser.py`** — поддержка альтернативных форматов сигналов, более надёжные паттерны
- [ ] **Метки времени** в БД — перевести с TEXT на INTEGER (Unix ms) для корректной сортировки

---

## Порядок выполнения

Рекомендуется выполнять тиры последовательно. Tier 1 не требует рефакторинга архитектуры — только точечные исправления. Tier 2–3 можно делать параллельно по модулям. Tier 4 — в конце как отдельные задачи.

Каждое изменение рекомендуется фиксировать отдельным коммитом с понятным описанием, чтобы сохранять возможность отката.

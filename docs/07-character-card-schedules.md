# Character Card Schedules (AubergeRP Extension)

AubergeRP adds a proactive scheduling feature via the Character Card V2 `extensions` mechanism.
This is an **AubergeRP-specific extension** and is not part of the core Character Card specification.

---

## Extension format

Schedules and proactive defaults are defined inside `data.extensions.aubergerp`:

```json
{
  "spec": "chara_card_v2",
  "spec_version": "2.0",
  "data": {
    "name": "Alice",
    "extensions": {
      "aubergerp": {
        "proactive": {
          "enabled": true,
          "decision_mode": "contextual",
          "minimum_cooldown_minutes": 180,
          "maximum_active_schedules_per_conversation": 20,
          "maximum_scheduling_horizon_minutes": 43200
        },
        "schedules": [
          {
            "id": "morning_checkin",
            "enabled": true,
            "type": "daily_window",
            "start": "09:00",
            "end": "11:00",
            "instruction": "Ask {{user}} how they slept and naturally mention your own night."
          },
          {
            "id": "evening_checkin",
            "enabled": true,
            "type": "daily_at",
            "time": "20:00",
            "instruction": "Check in with {{user}} about how their day went."
          }
        ]
      }
    }
  }
}
```

---

## Schedule definition fields

| Field         | Required | Description |
|---------------|----------|-------------|
| `id`          | ✔        | Unique identifier within this character. Used to link to runtime instances. |
| `enabled`     |          | Default `true`. Can be overridden per-user at runtime. |
| `type`        | ✔        | Schedule type. See [Schedule types](#schedule-types). |
| `time`        | ✔ (daily_at)     | Local trigger time in `HH:MM` format (24h). |
| `start`       | ✔ (daily_window) | Local window start in `HH:MM` format (24h). |
| `end`         | ✔ (daily_window) | Local window end in `HH:MM` format (24h). |
| `instruction` | ✔        | Natural-language goal for the character. See [Instruction field](#instruction-field). |

---

## Schedule types

### `daily_at`

Fires once per day at the specified local time.

```json
{
  "type": "daily_at",
  "time": "09:30"
}
```

### `daily_window`

Fires once per day at a randomly chosen time within a local time window.
This prevents all users from receiving messages at exactly the same moment and
makes the behavior feel more natural.

```json
{
  "type": "daily_window",
  "start": "09:00",
  "end": "11:00"
}
```

The window must have `end > start`.  A single trigger is chosen per day;
the character will not message the user multiple times within the window.

### `after_delay`

Fires after a delay from the latest user message in the conversation.

```json
{
  "type": "after_delay",
  "delay_minutes": 180,
  "not_before_time": "09:00"
}
```

### `after_inactivity`

Fires when the conversation remains inactive for a configured duration.

```json
{
  "type": "after_inactivity",
  "inactivity_minutes": 1440
}
```

Event-based triggers are re-based on each new user message and are restart-safe.

---

## Instruction field

The `instruction` tells the character **what to do**, not exactly what message to send.
AubergeRP injects it into the normal roleplay generation pipeline as a system message.

The character uses the full conversation history, character personality, relationship,
and memory — it generates a contextual, non-templated response.

Example instructions:

- `"Ask {{user}} how they slept and naturally mention your own night."`
- `"Check in warmly. Reference something from a recent conversation if possible."`
- `"Gently remind {{user}} to drink water."`

`{{user}}` is resolved to the user's configured name.

---

## Timezone semantics

- Schedule times are **user-local times** expressed in an IANA timezone (e.g. `Europe/Paris`).
- AubergeRP stores the IANA identifier, not a fixed UTC offset.
- DST transitions are handled automatically by the `zoneinfo` library.
- The user's timezone is stored per (channel, bot, user) via `/timezone` or the web API.
- If no timezone is configured for a user, `UTC` is used as the fallback.
- When a user's timezone is changed, the next trigger time is recalculated automatically.

---

## Card vs. runtime state separation

| What                      | Where stored                    |
|---------------------------|---------------------------------|
| Schedule definition       | Character card (`extensions.aubergerp.schedules`) |
| Enabled/disabled default  | Character card (`enabled` field) |
| Runtime enabled override  | `schedule_instances` DB table   |
| Last run time             | `schedule_instances` DB table   |
| Next run time             | `schedule_instances` DB table   |
| Generation lock           | `schedule_instances` DB table   |
| Timezone                  | `user_timezones` DB table       |

**Do not put runtime state inside the character card.**
The card holds the behavior definition; the database holds execution state.

---

## Import / export behavior

Character card schedules survive the full import → edit → export → re-import lifecycle:

- **Import**: `extensions.aubergerp.schedules` is read and preserved as-is.
- **Edit**: The admin character editor shows a Schedules section for adding, editing,
  enabling/disabling, and deleting schedule definitions.
- **Export** (JSON or PNG): `extensions.aubergerp.schedules` is included in the export.
- **Re-import**: Existing runtime schedule instances are not affected by re-import.
  New instances will be created the next time the character is accessed through a channel.

Cards without schedules are fully valid and are not affected by this feature.
Unknown extension keys are preserved on import and export.

---

## Runtime schedule instances

A single character schedule definition produces one **runtime instance** per
(character, schedule_def, conversation) combination:

```
Character Alice
  morning_checkin
      ├── User A / Telegram (bot 1)
      └── User B / Telegram (bot 1)
      └── User C / Web
```

Each instance has its own:
- Timezone (from the user's stored timezone)
- Enabled state (can differ from the card default)
- Last run time and next run time

### API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/schedules/instances/character/{id}` | List instances for a character |
| `GET`  | `/api/schedules/instances/conversation/{id}` | List instances for a conversation |
| `GET`  | `/api/schedules/instances/{id}` | Get a specific instance |
| `POST` | `/api/schedules/instances` | Create / get-or-create an instance |
| `PATCH`| `/api/schedules/instances/{id}/enabled` | Enable or disable a specific instance |
| `DELETE`| `/api/schedules/instances/{id}` | Delete an instance |

---

## Transport behavior

The scheduler is **transport-neutral**. The same code fires scheduled events for
both Telegram and Web sessions.

### Telegram

Proactive messages are delivered directly to the Telegram chat using the bot that
owns the session.

Telegram instances are automatically created the first time a user messages a bot
that has a character with schedules enabled.

### Web

Web sessions do not currently support server-initiated push delivery.
The generated assistant message is persisted to the conversation history normally.
It will appear the next time the user opens or refreshes the chat.

---

## Idempotency and duplicate prevention

AubergeRP guarantees that each scheduled occurrence produces **at most one**
assistant response, even across:

- server restarts
- multiple scheduler ticks observing the same due job
- delivery failures

The `generation_started_at` field acts as a lock:

1. Before generation: `generation_started_at` is set atomically.
2. If generation succeeds: `last_run_at` is updated, `next_run_at` is recalculated,
   and `generation_started_at` is cleared.
3. If generation fails: `generation_started_at` is cleared and the tick will retry.
4. If Telegram delivery fails: the assistant message is already in the conversation
   history. No new LLM call is made.

---

## Generation pipeline

Proactive messages go through the **normal AubergeRP roleplay engine**:

- Character personality, description, scenario
- Full conversation history
- Memory / summarization
- OOC protection disabled (the injection is internal, not user input)
- No user message is added for proactive events

An internal system message is injected containing:

- The current local time in the user's timezone
- The schedule instruction
- Instructions to not reveal the scheduling mechanism

---

## Proactive decision mode

When `proactive.decision_mode` is `contextual`, a fired trigger is evaluated by the LLM:

- `SEND(message)` → persist + deliver
- `SKIP(reason)` → execution recorded as skipped, no assistant message created

Execution outcomes are persisted as `sent`, `skipped`, or `failed`.

## Character tools

For tool-calling-capable LLM backends, characters can use:

- `schedule_proactive_message`
- `cancel_scheduled_message`
- `list_scheduled_messages`

Schedules created this way are stored as runtime definitions with origin `character-tool`.

## Limits and safety

`data.extensions.aubergerp.proactive` supports:

- `maximum_active_schedules_per_conversation`
- `minimum_cooldown_minutes`
- `maximum_scheduling_horizon_minutes`

Duplicate/near-identical schedules are suppressed per conversation.

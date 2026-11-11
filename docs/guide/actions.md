# Actions

For scripts, automations and troubleshooting. You never need any of these for
the integration to work: it already runs itself, and everything day-to-day has
a control on the [hub device](hub_controls.md) or a load's device.

Call an action from **Developer tools → Actions**, a script or an automation.
Actions marked *administrators only* are refused for a non-admin user; a call
with no user behind it (an automation, a script) is always allowed.

| Action | What it does | Fields | Who |
| --- | --- | --- | --- |
| [`emhass_companion.run_mpc`](#run_mpc) | Re-plans from the current state | none | anyone |
| [`emhass_companion.run_dayahead`](#run_dayahead) | Rebuilds the plan across the whole horizon | none | anyone |
| [`emhass_companion.train_load_forecaster`](#train_load_forecaster) | Retrains the machine-learning load forecaster | none | anyone |
| [`emhass_companion.set_hot_water_plan`](#set_hot_water_plan) | Replaces a hot-water tank's whole plan | `device_id`, `periods` | anyone |
| [`emhass_companion.upsert_on_demand_sessions`](#upsert_on_demand_sessions) | Creates or updates a batch of dated on-demand requests | `sessions` | anyone |
| [`emhass_companion.export_settings`](#export_settings) | Saves Companion's static settings into EMHASS | none | administrators |
| [`emhass_companion.test_profile`](#test_profile) | Shows what a data source profile returns | `profile`, `options`, `limit` | administrators |
| [`emhass_companion.typical_load_forecast`](#typical_load_forecast) | Builds the *Typical household* forecast | `day`, `average_w` | anyone |

## run_mpc

The **Recalculate now** button, as an action. Runs a model-predictive
optimisation from the current state instead of waiting for the next scheduled
one.

```yaml
action: emhass_companion.run_mpc
```

## run_dayahead

A full optimisation across the whole planning horizon. There is no button for
it, because it already runs by itself:

- at startup;
- when the price source gains roughly a new day of prices (Nord Pool
  publishes the next day around 13:00) — never more than once an hour;
- at the **Day-ahead fallback time** (13:30 by default), as a backstop, unless
  one just ran.

Call it yourself after a change you want reflected across the whole horizon
at once, or to retry after a failed run without waiting.

```yaml
action: emhass_companion.run_dayahead
```

## train_load_forecaster

Trains EMHASS's machine-learning load forecaster on your load sensor's recent
history. There is no button for it either. Only needed if your load forecast
method is *Machine-learning forecaster*; otherwise the action fails with an
explanation.

You rarely need it: the model trains itself once the sensor has enough history
(nine days), whenever the time step, load sensor or lag count changes, and
**every week** — a trained model does not keep learning from new data, so the
weekly retraining is what keeps it following the seasons. Call it to retrain
immediately, for example after a big change in how your household uses power.

```yaml
action: emhass_companion.train_load_forecaster
```

Training can take a while on a small host. If it fails, runs carry on with the
sensor's recent readings as the forecast until a later attempt succeeds.

## set_hot_water_plan

Replaces a hot-water tank's whole plan in one step. Fields, example and rules
are on [Thermal loads](thermal_loads.md#water-tank-with-an-immersion-heater).

## upsert_on_demand_sessions

For manual setup, choose **On demand with date** as the load's recurrence.
Set **Session start (draft)** and **Session end (draft)** on the load, set
its required operating hours, then press **Apply dated session**. The draft
fields do not alter the active request until the button is pressed. **Active
session start** and **Active session end** show the window currently used by
the optimiser. Applying edits to the same active session preserves progress.
Turn off Requested to cancel it before starting a different session.

Submit one or more dated sessions for existing Companion loads configured as
**On demand**. Every batch is validated before any state changes, then triggers
one coordinated refresh. Repeating the same `session_id` updates its window or
total target without clearing credited progress. A different ID requires the
Requested switch for the previous session to be turned off first. Completed
IDs remain terminal; use a new ID for the next session. Absolute windows are
hard UTC instants and take precedence over the load's standing wall-clock
window and relative `run_within` setting. A window outside the current horizon
is parked until a later run can see it.

```yaml
action: emhass_companion.upsert_on_demand_sessions
data:
  sessions:
    - device_id: "<COMPANION_LOAD_DEVICE_ID>"
      session_id: "vehicle-2026-11-12-morning"
      start_at: "2026-11-11T18:00:00+01:00"
      end_at: "2026-11-12T07:00:00+01:00"
      total_hours: 3.0
    - device_id: "<ANOTHER_LOAD_DEVICE_ID>"
      session_id: "dishwasher-2026-11-11"
      start_at: "2026-11-11T21:00:00+01:00"
      end_at: "2026-11-12T02:00:00+01:00"
      total_hours: 1.5
```

The `sessions` list has no fixed slot count and can target any number of
configured load devices in one optimization. A given device can occur only
once per batch, and a load supports one active dated session at a time. Times
must include an explicit UTC offset. `total_hours` is the total target; when an
adapter recalculates it, include already credited operation in that total.
The Requested switch exposes `session_id`, `session_start_at`, `session_end_at`,
`session_total_hours`, `session_credited_hours` and `session_remaining_hours` as
state attributes. If two configured loads share one control entity, only one
load owns its command and progress in a cycle; overlapping dated windows on
that actuator are rejected by the action.

When a dated window extends past the planning horizon, Companion sends the
part of the remaining demand that fits within this horizon; the total target
and completed progress are retained for later replans. If the remaining demand
cannot fit even before the actual session end, the load is parked and its
**Session planning problem** entity shows the warning. Turn on **Plan partially
when time is insufficient** to schedule the available time instead. The
warning remains visible because this cannot meet the complete target. Neither
setting permits operation outside the dated window.

## export_settings

*Administrators only.* Saves Companion's static settings into EMHASS's own
saved configuration, so EMHASS's web UI and manual runs there look like
Companion's. Companion's own runs never depend on it. It overwrites some EMHASS
settings, so read [Write settings to
EMHASS](hub_controls.md#write-settings-to-emhass) before using it.

```yaml
action: emhass_companion.export_settings
```

## test_profile

*Administrators only.* Resolves a data source profile and returns what it
produced, for when a source returns nothing or the wrong values. The response
includes the profile's resolved EMHASS settings, which can contain a forecast
API key, which is why it is restricted.

```yaml
action: emhass_companion.test_profile
data:
  profile: price/nordpool_custom
  options:
    entity: sensor.nordpool_kwh_se3_sek_3_10_025
  limit: 10   # how many points to return, 1–500
response_variable: result
```

See [Troubleshooting](../troubleshooting.md)
and [Writing a profile](../profiles.md).

## typical_load_forecast

Used internally by the *Typical household* load profile, which asks it for a
day's typical shape scaled to your average power. You don't need to call it;
it is listed because it shows up in **Developer tools → Actions**.

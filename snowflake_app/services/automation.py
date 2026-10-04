"""
Automation: what happens without anyone clicking.

  machine trips (protective stop)
      -> an emergency work order is raised by the runner (services/live.py)
      -> an 8D investigation is started or queued for the machine and linked to
         the work order                               (automation.auto_8d_on_trip)
      -> recipient lists marked "Alert when a machine goes down" get the work
         order by email                               (automation.notify_on_trip)
  an investigation finishes
      -> its corrective and preventive actions are tracked (capa_ops.ingest)
      -> recipient lists marked "Send every finished report" get the report

Hooks run on their own threads with the settings snapshot taken when
monitoring (or the run) started, so they work while nobody has the app open.
Every email lands in EMAIL_LOG, sent or failed.
"""

from snowflake_app.services import capa_ops, impact_ops, mailer
from snowflake_app.services import settings as S
from snowflake_app.services.llm import bound_complete


def on_trip(rm, mon, wo: dict, ev: dict, settings: dict):
    auto = settings.get("automation") or {}
    mid = wo["machine_id"]
    if auto.get("auto_8d_on_trip", True) and not wo.get("run_id"):
        try:
            trigger = mon.escalation_trigger(mid)
        except KeyError:
            trigger = {"source": "auto", "machine_id": mid}
        provider = S.describe_active(settings)
        try:
            res = rm.enqueue(trigger, bound_complete(settings), provider, label=f"{mid} protective stop",
                         context={"settings": settings, "reason": "trip", "wo_id": wo["wo_id"],
                                  "on_start": lambda run_id: mon.link_run(wo["wo_id"], run_id)})
            if res.get("run_id"):
                mon.link_run(wo["wo_id"], res["run_id"])
        except Exception as e:  # the alert email below must still go out
            with mon.lock:
                mon.engine._event("warning", mid, f"Could not start the 8D investigation for {wo['wo_id']}: "
                                  f"{str(e).splitlines()[0][:200]}", kind="automation_error")
    if auto.get("notify_on_trip", True):
        lists = [x for x in mailer.lists() if x.get("on_machine_down")]
        if lists and mailer.ready(settings)["ok"]:
            try:
                impact = impact_ops.for_machine(mid, mon.outlook(), mon.work_orders(), settings)
            except Exception:
                impact = None  # the alert goes out without the order list
            html = mailer.alert_html(wo, ev, impact)
            subject = f"Machine down: {mid} {wo.get('machine_name') or ''} · {wo['wo_id']}"
            for lst in lists:
                try:
                    mailer.send_html(settings, lst["emails"], subject, html, reason="machine down", list_name=lst["name"])
                except mailer.EmailError:
                    pass  # recorded in the email log


def on_report_complete(snap: dict, ctx: dict):
    try:
        capa_ops.ingest(snap)  # tracked whether or not anyone gets the report by email
    except Exception:
        pass
    settings = ctx.get("settings")
    if not settings or snap.get("status") != "completed":
        return
    lists = [x for x in mailer.lists() if x.get("on_report")]
    if not lists or not mailer.ready(settings)["ok"]:
        return
    reason = "auto: protective stop" if ctx.get("reason") == "trip" else "auto: report finished"
    for lst in lists:
        try:
            mailer.deliver(settings, snap, lst["emails"], list_name=lst["name"], reason=reason)
        except mailer.EmailError:
            pass  # recorded in the email log

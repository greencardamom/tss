#!/usr/bin/awk -f

#
# monitor_tss.awk - health monitor for the tss (Tarb Stats Server) Toolforge webservice.
#   Mirrors monitor_gcapi.awk / monitor_bup.awk. Runs from cron on acre.
#
#   Two-layer check, and the remediation differs by layer:
#     TSS_*  = the webservice/pod itself is down (web UI or /health) -> recycling the
#              pod can help: webservice restart, then forceful kubectl delete pod.
#     DB_*   = tss is alive but a ToolsDB-backed read (/api/v1/sources) is failing ->
#              the fault is the data backend; recycling a healthy pod is futile, so
#              LEAVE IT ALONE and just page (every cycle, by design).
#   The read API is public, so no secret/auth is needed. Every probe is timeout-bounded
#   so a wedged endpoint can't hang the monitor.
#
#   Cron (every 30 min, offset from gcapi 0,30 / bup :43):
#     15,45 * * * * /home/greenc/scripts/monitor_tss.awk
#
#   Log: /home/greenc/scripts/monitor_tss.log
#

@include "library"

# --- bounded curl helpers -------------------------------------------------
function http_code(Cfg, url) {
  return sys2var(Cfg["curl"] " -s -o /dev/null -w '%{http_code}' --max-time " Cfg["timeout"] " " shquote(url))
}
function http_body(Cfg, url) {
  return sys2var(Cfg["curl"] " -s --max-time " Cfg["timeout"] " " shquote(url))
}

# --- One full health probe. Returns "HEALTHY" or a short failure reason ---
function check_tss_health(Cfg,    code, body) {
  # 1. Web UI liveness (the dashboard at / -> 200)
  code = http_code(Cfg, Cfg["web_url"])
  if (code == "000") return "TSS_UNRESPONSIVE"
  if (code != "200") return "TSS_WEB_HTTP_" code

  # 2. App liveness: /api/v1/health is shallow (DB-free) -> 200 + {"status":"ok"}
  code = http_code(Cfg, Cfg["health_url"])
  if (code == "000") return "TSS_UNRESPONSIVE"
  if (code != "200") return "TSS_HEALTH_HTTP_" code
  body = http_body(Cfg, Cfg["health_url"])
  if (body !~ /"status"[ :]+"ok"/) return "TSS_UNHEALTHY"

  # 3. Backend: /api/v1/sources runs a ToolsDB query -> non-empty JSON array of sources
  code = http_code(Cfg, Cfg["backend_url"])
  if (code == "000") return "DB_UNRESPONSIVE"
  if (code != "200") return "DB_HTTP_" code
  body = http_body(Cfg, Cfg["backend_url"])
  if (body !~ /"(slug|description|name)"/) return "DB_EMPTY"

  return "HEALTHY"
}

# --- Re-confirm a failure to ride out transient blips ---------------------
function confirm_status(Cfg, tries,    i, status) {
  status = check_tss_health(Cfg)
  for (i = 1; i < tries && status != "HEALTHY"; i++) {
    system("sleep " Cfg["recheck_sleep"])
    status = check_tss_health(Cfg)
  }
  return status
}

# --- Run a remote SSH command; capture output + the REMOTE exit code -------
# Sets global RemoteRC (-1 if unreadable), so a silently-failed restart is visible.
function remote_run(remotecmd,    out, n, a, i) {
  out = sys2var("ssh -o BatchMode=yes -o ConnectTimeout=30 tools " \
                shquote(remotecmd " ; echo ___RC=$?") " 2>&1")
  RemoteRC = -1
  n = split(out, a, "\n")
  for (i = n; i >= 1; i--) if (a[i] ~ /^___RC=[0-9]+$/) { RemoteRC = substr(a[i], 7) + 0; break }
  gsub(/___RC=[0-9]+/, "", out); gsub(/[ \t\r\n]+/, " ", out)
  return strip(out)
}

BEGIN {
  Cfg["curl"] = "/usr/bin/curl"          # read API is public: no auth header needed
  Cfg["web_url"]     = "https://tss.toolforge.org/"
  Cfg["health_url"]  = "https://tss.toolforge.org/api/v1/health"
  Cfg["backend_url"] = "https://tss.toolforge.org/api/v1/sources"
  Cfg["timeout"]       = 20
  Cfg["confirm_tries"] = 3
  Cfg["recheck_sleep"] = 5

  # Self-heal (SSH to the tools bastion). 0 = alert-only.
  Cfg["restart_enabled"]    = 1
  Cfg["post_restart_sleep"] = 20
  Cfg["backoff_sleep"]      = 30

  MONLOG = "/home/greenc/scripts/monitor_tss.log"

  # --- STRIKE 1: confirm health -----------------------------------------
  status = confirm_status(Cfg, Cfg["confirm_tries"])
  if (status == "HEALTHY")
    healthcheckwatch()                       # all good (exits)

  log_event("WARNING", "tss unhealthy (" status ") after " Cfg["confirm_tries"] " probes.", MONLOG)

  # BACKEND fault: tss itself is alive (web + /health OK) but the ToolsDB-backed read
  # is failing -> data backend, not tss. Recycling can't fix ToolsDB; leave the pod
  # alone and just page (every cycle while it stays down, by design).
  if (status ~ /^DB_/)
    trigger_escalation(status, Cfg, "backend", MONLOG)   # emails + exits

  # tss fault (webservice/pod itself down) -> remediate.
  if (Cfg["restart_enabled"] != 1)
    trigger_escalation(status, Cfg, "tss", MONLOG)       # alert-only (exits)

  # --- STRIKE 2: remediate (remote restart, exit-checked, one retry) ----
  log_event("WARNING", "Attempting remote webservice restart...", MONLOG)
  out = remote_run("become tss webservice restart")
  log_event(RemoteRC == 0 ? "INFO" : "WARNING", "webservice restart exit=" RemoteRC " -- " out, MONLOG)
  if (RemoteRC != 0) {                          # transient bastion/control-plane failure -> retry once
    system("sleep " Cfg["recheck_sleep"])
    out = remote_run("become tss webservice restart")
    log_event(RemoteRC == 0 ? "INFO" : "WARNING", "webservice restart retry exit=" RemoteRC " -- " out, MONLOG)
  }
  system("sleep " Cfg["post_restart_sleep"])
  status = check_tss_health(Cfg)

  # --- STRIKE 3: force a pod recreation if the restart did not take -----
  if (status != "HEALTHY") {
    log_event("WARNING", "Still unhealthy (" status ") after restart; forcing pod delete...", MONLOG)
    pod = remote_run("become tss kubectl get pods -o name 2>/dev/null | head -1")
    if (pod ~ /^pod\//) {
      out = remote_run("become tss kubectl delete " pod)
      log_event(RemoteRC == 0 ? "INFO" : "WARNING", "kubectl delete " pod " exit=" RemoteRC " -- " out, MONLOG)
      system("sleep " Cfg["post_restart_sleep"])
    } else {
      log_event("WARNING", "could not resolve tss pod name (got: " pod ") -- skipping force-delete", MONLOG)
    }
    system("sleep " Cfg["backoff_sleep"])
    status = check_tss_health(Cfg)
    if (status != "HEALTHY")
      trigger_escalation(status, Cfg, "tss", MONLOG)     # remediation failed (exits)
  }

  # Recovered on strike 2 or 3.
  log_event("RECOVERED", "tss revived after restart. Status: " status, MONLOG)
  healthcheckwatch()
}

#
# Escalation: email + log + dead-man ping, then exit. kind = "tss" | "backend".
#
function trigger_escalation(final_status, Cfg, kind, MONLOG,    endtime, body, subj) {
  endtime = strftime("%Y-%m-%dT%H:%M:%S", systime(), 0)

  if (kind == "backend") {
    body = "monitor_tss.awk: tss data backend (ToolsDB) unresponsive (checked from acre).\n"
    body = body "Checked at: " endtime "\n\n"
    body = body "Status: " final_status "\n"
    body = body "tss itself is HEALTHY (web + /api/v1/health OK); a ToolsDB-backed read\n"
    body = body "(/api/v1/sources) is failing -- typically ToolsDB slowness/contention. The pod was\n"
    body = body "LEFT ALONE on purpose: recycling it cannot fix the database, only churn a healthy pod.\n\n"
    body = body "This alert repeats every cycle while the backend stays unresponsive (by design).\n"
    body = body "Check: ToolsDB availability/latency, or a slow read query.\n"
    subj = "TSS: DATA BACKEND UNRESPONSIVE (" final_status ")"
    log_event("FAILED", "tss data backend unresponsive (tss healthy): " final_status, MONLOG)
  } else {
    body = "monitor_tss.awk: tss webservice health check FAILED (from acre).\n"
    body = body "Checked at: " endtime "\n\n"
    body = body "Final failure reason: " final_status "\n\n"
    body = body "Endpoints:\n"
    body = body "  web    : " Cfg["web_url"] "\n"
    body = body "  health : " Cfg["health_url"] "\n"
    body = body "  sources: " Cfg["backend_url"] "\n\n"
    if (Cfg["restart_enabled"] == 1) {
      body = body "Remediation attempted (exit codes + output in " MONLOG "):\n"
      body = body "  1. become tss webservice restart (retried once on non-zero exit)\n"
      body = body "  2. forceful become tss kubectl delete <pod> (if still unhealthy)\n"
      body = body "Confirmed down (" Cfg["confirm_tries"] " probes); both steps ran; still down after "
      body = body Cfg["post_restart_sleep"] "s + " Cfg["backoff_sleep"] "s rechecks.\n\n"
    } else {
      body = body "Auto-restart disabled (alert-only); no remediation attempted.\n\n"
    }
    body = body "Check: become tss `webservice status` / `webservice logs` / `kubectl get pods`,\n"
    body = body "recent deploy, or NFS quota.\n"
    subj = "TSS WEBSERVICE DOWN (" final_status ")"
    log_event("FAILED", "tss down: " final_status, MONLOG)
  }

  email(Exe["from_email"], Exe["to_email"], subj, body)
  healthcheckwatch()
}

#
# Centralized logging helper
#
function log_event(type, message, MONLOG,    endtime, log_msg) {
  endtime = strftime("%Y-%m-%dT%H:%M:%S", systime(), 0)
  log_msg = "monitor_tss.awk [" type "]: " message
  gsub("\n", " ", log_msg)
  print endtime " - " log_msg >> MONLOG
  close(MONLOG)
}

#
# Ping HealthcheckWatch (dead-man's switch) and exit.
#
function healthcheckwatch() {
  hcw_ping("acre-monitor_tss", 2, "NOTIFY (HCW): monitor_tss.awk", "acre: /home/greenc/scripts/monitor_tss.awk (failed or silent)")
  exit
}

"""Tiny static server that proxies /dashboard.json from R2 to avoid CORS."""
import http.server
import socketserver
import os
import urllib.error
import urllib.request
import json
import threading
import re
import time
import gzip

R2_URL = "https://us-east-1.hippius.com/constantinople/sn66/dashboard.json"
R2_URL_FALLBACK = "https://s3.hippius.com/constantinople/sn66/dashboard.json"
DUEL_INDEX_URL = "https://us-east-1.hippius.com/constantinople/sn66/duels/index.json"
DUEL_INDEX_URL_FALLBACK = "https://s3.hippius.com/constantinople/sn66/duels/index.json"
DUEL_URL_TEMPLATE = "https://us-east-1.hippius.com/constantinople/sn66/duels/{duel_id}/duel.json"
DUEL_URL_FALLBACK_TEMPLATE = "https://s3.hippius.com/constantinople/sn66/duels/{duel_id}/duel.json"
PORT = int(os.environ.get("PORT", 80))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCAL_DASHBOARD_PATH = os.path.join(SCRIPT_DIR, "dashboard_data.json")
LOCAL_DASHBOARD_PATH = os.environ.get(
    "DASHBOARD_DATA_PATH",
    DEFAULT_LOCAL_DASHBOARD_PATH,
)
PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_URL", "https://s3.hippius.com/constantinople")


_dashboard_cache = {"data": None, "ts": 0}
_dashboard_summary_cache = {"data": None, "ts": 0}
_duel_index_cache = {"data": None, "ts": 0}
_duel_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 5
DUEL_INDEX_CACHE_TTL = 60
DUEL_CACHE_TTL = 60
MAX_DUEL_CACHE_ITEMS = 64
MAX_DASHBOARD_DUEL_LINK_ENRICH = 25
DUEL_PATH_RE = re.compile(r"^/duels/([0-9]{1,6})\.json$")
PR_COMMITMENT_RE = re.compile(r"^github-pr:(?P<repo>[^#@\s]+)#(?P<number>\d+)@(?P<sha>[0-9a-fA-F]+)$")
_duel_link_cache = {}


class UpstreamNotFound(Exception):
    pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 64


class Handler(http.server.SimpleHTTPRequestHandler):
    timeout = 15

    def end_headers(self):
        path = getattr(self, "path", None)
        if path == "/health":
            pass
        elif path == "/dashboard-summary.json":
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif path == "/dashboard.json":
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif path == "/duels/index.json":
            self.send_header("Cache-Control", "no-cache, max-age=60")
        elif path and DUEL_PATH_RE.match(path):
            self.send_header("Cache-Control", "no-cache, max-age=30")
        else:
            self.send_header("Cache-Control", "no-cache, max-age=60")
        super().end_headers()

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        pass

    def _send_bytes(self, data, content_type, status=200, cors=False):
        if isinstance(data, str):
            data = data.encode()
        headers = {}
        accept_encoding = self.headers.get("Accept-Encoding", "")
        if len(data) > 1024 and "gzip" in accept_encoding.lower():
            data = gzip.compress(data, compresslevel=5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_local_dashboard(self):
        try:
            with open(LOCAL_DASHBOARD_PATH, "rb") as f:
                data = f.read()
            payload = json.loads(data)
            if not isinstance(payload, dict) or "duels" not in payload:
                return None
            return self._augment_dashboard_links(data)
        except Exception:
            return None

    def _duel_file_path(self, duel_id):
        return os.path.join(
            os.path.dirname(LOCAL_DASHBOARD_PATH),
            "duels",
            f"{int(duel_id):06d}.json",
        )

    def _state_file_path(self):
        return os.path.join(os.path.dirname(LOCAL_DASHBOARD_PATH), "state.json")

    def _participant_pr_url(self, participant):
        if not isinstance(participant, dict):
            return None
        if participant.get("pr_url"):
            return str(participant["pr_url"])
        base_repo = participant.get("base_repo_full_name")
        pr_number = participant.get("pr_number")
        commitment = str(participant.get("commitment") or "")
        if (not base_repo or pr_number is None) and commitment:
            match = PR_COMMITMENT_RE.fullmatch(commitment.strip())
            if match:
                base_repo = match.group("repo")
                pr_number = match.group("number")
        if not base_repo or pr_number is None:
            return None
        try:
            return f"https://github.com/{base_repo}/pull/{int(pr_number)}"
        except (TypeError, ValueError):
            return None

    def _duel_pr_urls(self, duel_id):
        path = self._duel_file_path(duel_id)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}
        cache_key = str(duel_id).zfill(6)
        cached = _duel_link_cache.get(cache_key)
        if cached and cached.get("mtime") == mtime:
            return cached.get("links", {})
        try:
            with open(path, "rb") as f:
                payload = json.loads(f.read())
        except Exception:
            return {}
        links = {
            "king": self._participant_pr_url(
                payload.get("king_before") or payload.get("king") or payload.get("king_after")
            ),
            "challenger": self._participant_pr_url(payload.get("challenger")),
        }
        links = {key: value for key, value in links.items() if value}
        _duel_link_cache[cache_key] = {"mtime": mtime, "links": links}
        return links

    def _active_duel_pr_urls(self, duel_id):
        try:
            with open(self._state_file_path(), "rb") as f:
                state = json.loads(f.read())
        except Exception:
            return {}
        active = state.get("active_duel") if isinstance(state, dict) else None
        if not isinstance(active, dict):
            return {}
        if str(active.get("duel_id")) != str(duel_id):
            return {}
        links = {
            "king": self._participant_pr_url(active.get("king")),
            "challenger": self._participant_pr_url(active.get("challenger")),
        }
        return {key: value for key, value in links.items() if value}

    def _prefer_pr_url(self, payload):
        if not isinstance(payload, dict):
            return
        pr_url = payload.get("pr_url")
        if pr_url:
            payload["repo_url"] = str(pr_url)
            payload["display_repo_url"] = str(pr_url)

    def _copy_fields(self, source, fields):
        if not isinstance(source, dict):
            return None
        return {field: source.get(field) for field in fields if field in source}

    def _round_count(self, source):
        if not isinstance(source, dict):
            return 0
        count = source.get("round_count")
        if isinstance(count, int) and count >= 0:
            return count
        rounds = source.get("rounds")
        return len(rounds) if isinstance(rounds, list) else 0

    def _submission_summary(self, source):
        fields = (
            "uid",
            "hotkey",
            "repo",
            "repo_full_name",
            "repo_url",
            "pr_url",
            "pr_number",
            "commit_sha",
            "display_repo_full_name",
            "display_repo_url",
            "display_commit_sha",
            "runtime_commit_sha",
            "runtime_repo_full_name",
            "runtime_repo_url",
            "source",
            "share",
            "commitment",
            "commitment_block",
            "base_repo_full_name",
        )
        summary = self._copy_fields(source, fields)
        if summary is not None:
            self._prefer_pr_url(summary)
        return summary

    def _round_summary(self, source):
        if not isinstance(source, dict):
            return {}
        return self._copy_fields(source, ("task_name", "winner", "llm_judge_winner", "error")) or {}

    def _duel_summary(self, source):
        fields = (
            "duel_id",
            "started_at",
            "finished_at",
            "king_replaced",
            "disqualification_reason",
            "confirmation_duel_id",
            "confirmation_retest_passed",
            "confirmation_failure_reason",
            "confirmation_of_duel_id",
            "manual_retest_of_duel_id",
            "wins",
            "losses",
            "ties",
            "threshold",
            "duel_rounds",
            "king_uid",
            "king_hotkey",
            "king_repo",
            "king_repo_url",
            "king_pr_url",
            "king_commit_sha",
            "king_commitment_block",
            "challenger_uid",
            "challenger_hotkey",
            "hotkey",
            "challenger_repo",
            "challenger_repo_url",
            "challenger_pr_url",
            "challenger_commit_sha",
            "challenger_commitment_block",
        )
        summary = self._copy_fields(source, fields) or {}
        summary["round_count"] = self._round_count(source)
        return summary

    def _active_duel_summary(self, source):
        fields = (
            "duel_id",
            "phase",
            "status",
            "challenger_uid",
            "challenger_hotkey",
            "challenger_repo",
            "challenger_repo_url",
            "challenger_pr_url",
            "king_uid",
            "king_hotkey",
            "king_repo",
            "king_repo_url",
            "king_pr_url",
            "duel_rounds",
            "target_round_count",
            "gathered_tasks",
            "needed_tasks",
            "wins",
            "losses",
            "ties",
            "threshold",
            "task_set_phase",
            "confirmation_of_duel_id",
            "confirmation_duel_id",
            "manual_retest_of_duel_id",
            "pool_size",
        )
        summary = self._copy_fields(source, fields)
        if summary is None:
            return None
        rounds = source.get("rounds") if isinstance(source, dict) else None
        summary["rounds"] = [self._round_summary(item) for item in rounds] if isinstance(rounds, list) else []
        return summary

    def _status_summary(self, source):
        fields = (
            "netuid",
            "total_rounds",
            "miners_seen",
            "king_duels_defended",
            "king_since",
            "validator_started_at",
            "scoring",
            "links",
        )
        summary = self._copy_fields(source, fields) or {}
        summary["recent_kings"] = [
            self._submission_summary(item)
            for item in source.get("recent_kings", [])
            if isinstance(item, dict)
        ]
        summary["queue"] = [
            self._submission_summary(item)
            for item in source.get("queue", [])
            if isinstance(item, dict)
        ]
        summary["disqualified"] = [
            self._submission_summary(item)
            for item in source.get("disqualified", [])
            if isinstance(item, dict)
        ]
        summary["retired"] = [
            self._submission_summary(item)
            for item in source.get("retired", [])
            if isinstance(item, dict)
        ]
        summary["active_duel"] = self._active_duel_summary(source.get("active_duel"))
        return summary

    def _summarize_dashboard_payload(self, payload):
        if not isinstance(payload, dict):
            return payload
        links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
        return {
            "updated_at": payload.get("updated_at"),
            "current_king": self._submission_summary(payload.get("current_king")),
            "duels": [
                self._duel_summary(item)
                for item in payload.get("duels", [])
                if isinstance(item, dict)
            ],
            "status": self._status_summary(payload.get("status") or {}),
            "links": {**links, "duels_html": "./duels.html"},
        }

    def _augment_dashboard_links(self, data):
        try:
            payload = json.loads(data)
        except Exception:
            return data
        if not isinstance(payload, dict):
            return data

        links = payload.setdefault("links", {})
        if isinstance(links, dict):
            links["duels_html"] = "./duels.html"
        status = payload.get("status")
        if isinstance(status, dict):
            status_links = status.setdefault("links", {})
            if isinstance(status_links, dict):
                status_links["duels_html"] = "./duels.html"

        self._prefer_pr_url(payload.get("current_king"))
        if isinstance(status, dict):
            for item in status.get("recent_kings") or []:
                self._prefer_pr_url(item)
            for item in status.get("queue") or []:
                self._prefer_pr_url(item)
            active = status.get("active_duel")
            if isinstance(active, dict) and active.get("duel_id") is not None:
                links = self._active_duel_pr_urls(active["duel_id"])
                for role in ("king", "challenger"):
                    pr_url = active.get(f"{role}_pr_url") or links.get(role)
                    if pr_url:
                        active[f"{role}_pr_url"] = str(pr_url)
                        active[f"{role}_repo_url"] = str(pr_url)

        summaries = payload.get("duels") or []
        enrich_start = max(0, len(summaries) - MAX_DASHBOARD_DUEL_LINK_ENRICH)
        for index, summary in enumerate(summaries):
            if not isinstance(summary, dict) or summary.get("duel_id") is None:
                continue
            links = self._duel_pr_urls(summary["duel_id"]) if index >= enrich_start else {}
            for role in ("king", "challenger"):
                pr_url = summary.get(f"{role}_pr_url") or links.get(role)
                if pr_url:
                    summary[f"{role}_pr_url"] = str(pr_url)
                    summary[f"{role}_repo_url"] = str(pr_url)

        return json.dumps(payload, separators=(",", ":")).encode()

    def _build_duel_index_from_dashboard(self, dashboard_data):
        payload = json.loads(dashboard_data)
        entries = []
        for summary in payload.get("duels", []):
            if not isinstance(summary, dict) or summary.get("duel_id") is None:
                continue
            duel_id = int(summary["duel_id"])
            round_names = []
            for round_item in summary.get("rounds", []):
                if isinstance(round_item, dict):
                    round_names.append(round_item.get("task_name", ""))
                else:
                    round_names.append(str(round_item or ""))
            entries.append({
                "duel_id": duel_id,
                "started_at": summary.get("started_at"),
                "finished_at": summary.get("finished_at"),
                "king_repo": summary.get("king_repo"),
                "challenger_repo": summary.get("challenger_repo"),
                "king_repo_url": summary.get("king_repo_url"),
                "challenger_repo_url": summary.get("challenger_repo_url"),
                "king_pr_url": summary.get("king_pr_url"),
                "challenger_pr_url": summary.get("challenger_pr_url"),
                "king_uid": summary.get("king_uid"),
                "challenger_uid": summary.get("challenger_uid"),
                "king_hotkey": summary.get("king_hotkey"),
                "challenger_hotkey": summary.get("challenger_hotkey"),
                "king_replaced": summary.get("king_replaced", False),
                "disqualification_reason": summary.get("disqualification_reason"),
                "confirmation_duel_id": summary.get("confirmation_duel_id"),
                "confirmation_of_duel_id": summary.get("confirmation_of_duel_id"),
                "confirmation_retest_passed": summary.get("confirmation_retest_passed"),
                "confirmation_failure_reason": summary.get("confirmation_failure_reason"),
                "manual_retest_of_duel_id": summary.get("manual_retest_of_duel_id"),
                "wins": summary.get("wins", 0),
                "losses": summary.get("losses", 0),
                "ties": summary.get("ties", 0),
                "errors": summary.get("errors", 0),
                "rounds": round_names,
                "path": f"sn66/duels/{duel_id:06d}/",
            })
        return json.dumps({
            "updated_at": payload.get("updated_at"),
            "public_base_url": PUBLIC_BASE_URL,
            "current_king": self._submission_summary(payload.get("current_king")),
            "scoring": payload.get("status", {}).get("scoring", {}),
            "links": {"duels_html": "./duels.html"},
            "duels": entries,
        }).encode()

    def _fetch_dashboard(self):
        now = time.monotonic()
        with _cache_lock:
            if _dashboard_cache["data"] and (now - _dashboard_cache["ts"]) < CACHE_TTL:
                return _dashboard_cache["data"]
        local_data = self._read_local_dashboard()
        if local_data is not None:
            with _cache_lock:
                _dashboard_cache["data"] = local_data
                _dashboard_cache["ts"] = now
            return local_data
        # us-east-1 is ~2.5x faster from our vantage; fall back to s3.hippius.com
        # on any error so a regional hiccup doesn't kill the dashboard.
        last_err = None
        for url, timeout in ((R2_URL, 8), (R2_URL_FALLBACK, 10)):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "dash2.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                break
            except Exception as e:
                last_err = e
                data = None
        if data is None:
            with _cache_lock:
                if _dashboard_cache["data"]:
                    return _dashboard_cache["data"]
            raise last_err if last_err else RuntimeError("dashboard fetch failed")
        with _cache_lock:
            _dashboard_cache["data"] = data
            _dashboard_cache["ts"] = now
        return data

    def _fetch_dashboard_summary(self):
        now = time.monotonic()
        with _cache_lock:
            if _dashboard_summary_cache["data"] and (now - _dashboard_summary_cache["ts"]) < CACHE_TTL:
                return _dashboard_summary_cache["data"]

        dashboard_data = self._fetch_dashboard()
        payload = json.loads(dashboard_data)
        summary = self._summarize_dashboard_payload(payload)
        data = json.dumps(summary, separators=(",", ":")).encode()
        with _cache_lock:
            _dashboard_summary_cache["data"] = data
            _dashboard_summary_cache["ts"] = now
        return data

    def _fetch_duel_index(self):
        now = time.monotonic()
        with _cache_lock:
            if _duel_index_cache["data"] and (now - _duel_index_cache["ts"]) < DUEL_INDEX_CACHE_TTL:
                return _duel_index_cache["data"]

        dashboard_data = self._fetch_dashboard()
        try:
            data = self._build_duel_index_from_dashboard(dashboard_data)
        except Exception:
            data = None
        if data is not None:
            with _cache_lock:
                _duel_index_cache["data"] = data
                _duel_index_cache["ts"] = now
            return data

        last_err = None
        for url, timeout in ((DUEL_INDEX_URL, 8), (DUEL_INDEX_URL_FALLBACK, 10)):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "dash2.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                break
            except Exception as e:
                last_err = e
                data = None
        if data is None:
            raise last_err if last_err else RuntimeError("duel index fetch failed")

        with _cache_lock:
            _duel_index_cache["data"] = data
            _duel_index_cache["ts"] = now
        return data

    def _fetch_duel(self, duel_id):
        now = time.monotonic()
        with _cache_lock:
            cached = _duel_cache.get(duel_id)
            if cached and (now - cached["ts"]) < DUEL_CACHE_TTL:
                return cached["data"]

        last_err = None
        not_found_count = 0
        attempt_count = 0
        for template, timeout in ((DUEL_URL_TEMPLATE, 8), (DUEL_URL_FALLBACK_TEMPLATE, 10)):
            attempt_count += 1
            try:
                url = template.format(duel_id=duel_id)
                req = urllib.request.Request(url, headers={"User-Agent": "dash2.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404:
                    not_found_count += 1
                data = None
            except Exception as e:
                last_err = e
                data = None
        if data is None:
            local_path = os.path.join(SCRIPT_DIR, "duels", f"{duel_id}.json")
            if os.path.isfile(local_path):
                with open(local_path, "rb") as f:
                    data = f.read()
            elif attempt_count and not_found_count == attempt_count:
                raise UpstreamNotFound(f"duel artifact {duel_id} not found")
        if data is None:
            raise last_err if last_err else RuntimeError("duel fetch failed")

        with _cache_lock:
            if len(_duel_cache) >= MAX_DUEL_CACHE_ITEMS:
                oldest = min(_duel_cache, key=lambda key: _duel_cache[key]["ts"])
                _duel_cache.pop(oldest, None)
            _duel_cache[duel_id] = {"data": data, "ts": now}
        return data

    def do_GET(self):
        if self.path == "/health":
            self._send_bytes(b"ok", "text/plain")
            return
        if self.path == "/dashboard.json":
            try:
                data = self._fetch_dashboard()
                self._send_bytes(data, "application/json", cors=True)
            except Exception as e:
                self._send_bytes(json.dumps({"error": "dashboard unavailable"}).encode(), "application/json", status=502)
            return
        if self.path == "/dashboard-summary.json":
            try:
                data = self._fetch_dashboard_summary()
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "dashboard summary unavailable"}).encode(), "application/json", status=502)
            return
        if self.path == "/duels/index.json":
            try:
                data = self._fetch_duel_index()
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "duel index unavailable"}).encode(), "application/json", status=502)
            return
        match = DUEL_PATH_RE.match(self.path)
        if match:
            duel_id = match.group(1).zfill(6)
            try:
                data = self._fetch_duel(duel_id)
                self._send_bytes(data, "application/json", cors=True)
            except UpstreamNotFound:
                self._send_bytes(json.dumps({"error": "duel artifact not found"}).encode(), "application/json", status=404, cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "duel artifact unavailable"}).encode(), "application/json", status=502)
            return
        return super().do_GET()

if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Serving on http://0.0.0.0:{PORT} (threaded)")
        httpd.serve_forever()

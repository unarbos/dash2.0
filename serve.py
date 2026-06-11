"""Tiny static server that proxies /dashboard.json from R2 to avoid CORS."""
import http.server
import socketserver
import os
import urllib.error
import urllib.parse
import urllib.request
import json
import threading
import re
import time
import gzip
try:
    import brotli
except Exception:
    brotli = None

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
DEFAULT_SWEBENCH_BENCHMARK_ROOT = "/home/const/subnet66/tau/workspace/validate/netuid-66/benchmarks/swebench-verified"
SWEBENCH_BENCHMARK_ROOT = os.environ.get(
    "SWEBENCH_BENCHMARK_ROOT",
    DEFAULT_SWEBENCH_BENCHMARK_ROOT,
)
PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_URL", "https://s3.hippius.com/constantinople")
SUBMISSIONS_API_UPSTREAM = os.environ.get(
    "SUBMISSIONS_API_UPSTREAM",
    "http://127.0.0.1:8066/api/submissions",
)


_dashboard_cache = {"data": None, "ts": 0}
_dashboard_summary_cache = {"data": None, "ts": 0, "local_mtime": None}
_dashboard_home_cache = {"data": None, "ts": 0, "local_mtime": None}
_dashboard_payload_cache = {"payload": None, "local_mtime": None}
_swebench_cache = {"data": None, "ts": 0, "latest_mtime": None}
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
        route = (path or "").split("?", 1)[0].rstrip("/") or "/"
        if route == "/health":
            pass
        elif route == "/api/submissions":
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif route in ("/dashboard-home.json", "/dashboard-summary.json"):
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif route == "/dashboard.json":
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif route == "/swebench-local.json":
            self.send_header("Cache-Control", "no-cache, max-age=0")
        elif route == "/duels/index.json":
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
        if len(data) > 1024 and brotli is not None and "br" in accept_encoding.lower():
            data = brotli.compress(data, quality=5)
            headers["Content-Encoding"] = "br"
            headers["Vary"] = "Accept-Encoding"
        elif len(data) > 1024 and "gzip" in accept_encoding.lower():
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

    def _is_submissions_api_path(self):
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        return route == "/api/submissions"

    def _proxy_submissions_api(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        upstream_url = SUBMISSIONS_API_UPSTREAM.rstrip("/")
        if parsed.query:
            upstream_url = f"{upstream_url}?{parsed.query}"

        body = None
        if method in {"POST", "PUT", "PATCH"}:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            body = self.rfile.read(max(0, length))

        headers = {}
        for name in ("Content-Type", "Accept", "User-Agent"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers["X-Forwarded-For"] = self.client_address[0]
        if self.headers.get("Host"):
            headers["X-Forwarded-Host"] = self.headers.get("Host")
        headers["X-Forwarded-Proto"] = "https" if self.headers.get("CF-Visitor") else "http"

        request = urllib.request.Request(
            upstream_url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=310) as response:
                content_type = response.headers.get("Content-Type", "application/json")
                self._send_bytes(response.read(), content_type, status=response.status, cors=True)
        except urllib.error.HTTPError as exc:
            content_type = exc.headers.get("Content-Type", "application/json")
            self._send_bytes(exc.read(), content_type, status=exc.code, cors=True)
        except Exception:
            self._send_bytes(
                json.dumps({"error": "submission API unavailable"}).encode(),
                "application/json",
                status=502,
                cors=True,
            )

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

    def _read_json_file(self, path):
        with open(path, "rb") as f:
            return json.loads(f.read())

    def _latest_swebench_path(self):
        return os.path.join(SWEBENCH_BENCHMARK_ROOT, "latest.json")

    def _mini_swe_usage_from_trajectories(self, outputs_dir):
        if not os.path.isdir(outputs_dir):
            return None
        totals = {
            "cost": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "trajectory_count": 0,
        }
        for name in sorted(os.listdir(outputs_dir)):
            trajectory_path = os.path.join(outputs_dir, name, "trajectory.json")
            if not os.path.isfile(trajectory_path):
                continue
            try:
                payload = self._read_json_file(trajectory_path)
            except Exception:
                continue
            totals["trajectory_count"] += 1
            for item in self._walk_json_objects(payload):
                usage = item.get("usage") if isinstance(item, dict) else None
                if not isinstance(usage, dict):
                    continue
                cost = usage.get("cost")
                if isinstance(cost, (int, float)):
                    totals["cost"] += float(cost)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, (int, float)):
                        totals[key] += int(value)
        if totals["trajectory_count"] == 0:
            return None
        totals["cost_available"] = totals["cost"] > 0
        return totals

    def _mini_swe_usage_from_comparison(self, comparison):
        king_sha = str(comparison.get("king_commit_sha") or "")
        if not king_sha:
            return None
        outputs_dir = os.path.join(
            SWEBENCH_BENCHMARK_ROOT,
            king_sha,
            "mini-swe-agent",
            "mini_outputs",
        )
        local_usage = self._mini_swe_usage_from_trajectories(outputs_dir)
        if local_usage is not None:
            return local_usage
        scores = comparison.get("scores") if isinstance(comparison.get("scores"), dict) else {}
        baseline_score = scores.get("baseline") if isinstance(scores.get("baseline"), dict) else scores.get("pi")
        report_path = str((baseline_score or {}).get("report_path") or "")
        official_dir = os.path.dirname(report_path)
        if os.path.basename(official_dir) != "official_scoring":
            return None
        return self._mini_swe_usage_from_trajectories(os.path.join(os.path.dirname(official_dir), "mini_outputs"))

    def _walk_json_objects(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json_objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json_objects(child)

    def _compact_swebench_payload(self, comparison):
        if not isinstance(comparison, dict):
            return {}
        scores = comparison.get("scores") if isinstance(comparison.get("scores"), dict) else {}
        usage = comparison.get("usage") if isinstance(comparison.get("usage"), dict) else {}
        baseline = comparison.get("baseline") if isinstance(comparison.get("baseline"), dict) else comparison.get("pi")
        mini_usage = self._mini_swe_usage_from_comparison(comparison)
        baseline_usage = usage.get("baseline") if isinstance(usage.get("baseline"), dict) else usage.get("pi")
        return {
            "status": comparison.get("status"),
            "benchmark": comparison.get("benchmark"),
            "started_at": comparison.get("started_at"),
            "finished_at": comparison.get("finished_at"),
            "king_commit_sha": comparison.get("king_commit_sha"),
            "king": comparison.get("king"),
            "baseline": baseline,
            "model": comparison.get("model"),
            "provider_only": comparison.get("provider_only"),
            "manifest": comparison.get("manifest"),
            "elapsed": comparison.get("elapsed"),
            "scores": scores,
            "usage": {
                "king": usage.get("king"),
                "baseline": mini_usage or baseline_usage,
                "cost_available": bool(
                    usage.get("king", {}).get("cost_available")
                    and (mini_usage or baseline_usage or {}).get("cost_available")
                ),
            },
        }

    def _line_count(self, path):
        try:
            with open(path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _compact_swebench_job(self, job_path):
        try:
            job = self._read_json_file(job_path)
        except Exception:
            return None
        if not isinstance(job, dict):
            return None
        job_dir = os.path.dirname(job_path)
        king = job.get("king") if isinstance(job.get("king"), dict) else {}
        comparison = job.get("comparison") if isinstance(job.get("comparison"), dict) else None
        payload = self._compact_swebench_payload(comparison) if comparison else {}
        payload.update({
            "status": job.get("status") or payload.get("status"),
            "started_at": job.get("started_at") or payload.get("started_at"),
            "updated_at": job.get("updated_at"),
            "error": job.get("error"),
            "king": payload.get("king") or king,
            "king_commit_sha": payload.get("king_commit_sha") or king.get("commit_sha"),
            "progress": {
                "baseline_solve_results": self._line_count(os.path.join(job_dir, "mini-swe-agent", "solve_results.jsonl")),
                "baseline_predictions": self._line_count(os.path.join(job_dir, "mini-swe-agent", "predictions.jsonl")),
                "king_solve_results": self._line_count(os.path.join(job_dir, "king", "solve_results.jsonl")),
                "king_predictions": self._line_count(os.path.join(job_dir, "king", "predictions.jsonl")),
                "total": 50,
            },
        })
        return payload

    def _latest_swebench_job(self):
        if not os.path.isdir(SWEBENCH_BENCHMARK_ROOT):
            return None
        jobs = []
        for name in os.listdir(SWEBENCH_BENCHMARK_ROOT):
            job_path = os.path.join(SWEBENCH_BENCHMARK_ROOT, name, "job.json")
            try:
                jobs.append((os.path.getmtime(job_path), job_path))
            except OSError:
                continue
        if not jobs:
            return None
        return self._compact_swebench_job(max(jobs, key=lambda item: item[0])[1])

    def _fetch_swebench_local(self):
        latest_path = self._latest_swebench_path()
        try:
            latest_mtime = os.path.getmtime(latest_path)
        except OSError:
            latest_mtime = None
        now = time.monotonic()
        with _cache_lock:
            if (
                _swebench_cache["data"] is not None
                and _swebench_cache["latest_mtime"] == latest_mtime
                and (now - _swebench_cache["ts"]) < CACHE_TTL
            ):
                return _swebench_cache["data"]
        if latest_mtime is None:
            data = json.dumps({"latest": None, "active": self._latest_swebench_job()}).encode()
        else:
            comparison = self._read_json_file(latest_path)
            data = json.dumps({
                "latest": self._compact_swebench_payload(comparison),
                "active": self._latest_swebench_job(),
            }).encode()
        with _cache_lock:
            _swebench_cache["data"] = data
            _swebench_cache["ts"] = now
            _swebench_cache["latest_mtime"] = latest_mtime
        return data

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
            "agent_username",
            "coldkey",
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
            "king_since",
            "king_duels_defended",
            "hold_seconds",
            "accepted_at",
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
            "king_agent_username",
            "king_repo",
            "king_repo_url",
            "king_pr_url",
            "king_commit_sha",
            "king_commitment_block",
            "challenger_uid",
            "challenger_hotkey",
            "challenger_agent_username",
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
            "challenger_agent_username",
            "challenger_repo",
            "challenger_repo_url",
            "challenger_pr_url",
            "king_uid",
            "king_hotkey",
            "king_agent_username",
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

    def _summarize_home_payload(self, payload):
        if not isinstance(payload, dict):
            return payload
        duels = payload.get("duels", [])
        recent_duels = duels[-40:] if isinstance(duels, list) else []
        links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
        return {
            "updated_at": payload.get("updated_at"),
            "current_king": self._submission_summary(payload.get("current_king")),
            "duels": [
                self._duel_summary(item)
                for item in recent_duels
                if isinstance(item, dict)
            ],
            "duels_total": len(duels) if isinstance(duels, list) else 0,
            "status": self._status_summary(payload.get("status") or {}),
            "links": {**links, "duels_html": "./duels.html", "dashboard_summary": "./dashboard-summary.json"},
        }

    def _dashboard_payload_from_local_or_remote(self):
        local_mtime = None
        try:
            local_mtime = os.path.getmtime(LOCAL_DASHBOARD_PATH)
        except Exception:
            local_mtime = None

        with _cache_lock:
            cached = _dashboard_payload_cache.get("payload")
            if cached is not None and local_mtime is not None and _dashboard_payload_cache.get("local_mtime") == local_mtime:
                return cached, local_mtime

        try:
            with open(LOCAL_DASHBOARD_PATH, "rb") as f:
                payload = json.loads(f.read())
        except Exception:
            dashboard_data = self._fetch_dashboard()
            payload = json.loads(dashboard_data)

        with _cache_lock:
            _dashboard_payload_cache["payload"] = payload
            _dashboard_payload_cache["local_mtime"] = local_mtime
        return payload, local_mtime

    def _json_cache_for_dashboard(self, cache, summarizer):
        now = time.monotonic()
        with _cache_lock:
            if cache["data"] and (now - cache["ts"]) < CACHE_TTL:
                return cache["data"]

        local_mtime = None
        try:
            local_mtime = os.path.getmtime(LOCAL_DASHBOARD_PATH)
        except Exception:
            local_mtime = None
        with _cache_lock:
            if cache["data"] and local_mtime is not None and cache.get("local_mtime") == local_mtime:
                cache["ts"] = now
                return cache["data"]

        payload, local_mtime = self._dashboard_payload_from_local_or_remote()
        data = json.dumps(summarizer(payload), separators=(",", ":")).encode()
        with _cache_lock:
            cache["data"] = data
            cache["ts"] = now
            cache["local_mtime"] = local_mtime
        return data

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
        return self._json_cache_for_dashboard(_dashboard_summary_cache, self._summarize_dashboard_payload)

    def _fetch_dashboard_home(self):
        return self._json_cache_for_dashboard(_dashboard_home_cache, self._summarize_home_payload)

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

    def _paginate_duel_index(self, data):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            limit = int((params.get("limit") or ["0"])[0])
        except (TypeError, ValueError):
            limit = 0
        before = (params.get("before") or [""])[0]
        if limit <= 0 and not before:
            return data

        payload = json.loads(data)
        duels = payload.get("duels", [])
        if before:
            try:
                before_id = int(before)
                duels = [item for item in duels if int(item.get("duel_id", 0)) < before_id]
            except (TypeError, ValueError):
                pass
        if limit > 0:
            duels = duels[-limit:]

        payload["duels"] = duels
        payload["page"] = {
            "limit": limit or None,
            "before": before or None,
            "count": len(duels),
            "next_before": min((int(item.get("duel_id", 0)) for item in duels), default=0) or None,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

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
        route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if route == "/health":
            self._send_bytes(b"ok", "text/plain")
            return
        if self._is_submissions_api_path():
            self._proxy_submissions_api("GET")
            return
        if route == "/dashboard.json":
            try:
                data = self._fetch_dashboard()
                self._send_bytes(data, "application/json", cors=True)
            except Exception as e:
                self._send_bytes(json.dumps({"error": "dashboard unavailable"}).encode(), "application/json", status=502)
            return
        if route == "/dashboard-home.json":
            try:
                data = self._fetch_dashboard_home()
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "dashboard home unavailable"}).encode(), "application/json", status=502)
            return
        if route == "/dashboard-summary.json":
            try:
                data = self._fetch_dashboard_summary()
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "dashboard summary unavailable"}).encode(), "application/json", status=502)
            return
        if route == "/swebench-local.json":
            try:
                data = self._fetch_swebench_local()
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "swebench local unavailable"}).encode(), "application/json", status=502)
            return
        if route == "/duels/index.json":
            try:
                data = self._paginate_duel_index(self._fetch_duel_index())
                self._send_bytes(data, "application/json", cors=True)
            except Exception:
                self._send_bytes(json.dumps({"error": "duel index unavailable"}).encode(), "application/json", status=502)
            return
        match = DUEL_PATH_RE.match(route)
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

    def do_OPTIONS(self):
        if self._is_submissions_api_path():
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_OPTIONS()

    def do_POST(self):
        if self._is_submissions_api_path():
            self._proxy_submissions_api("POST")
            return
        return super().do_POST()

if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Serving on http://0.0.0.0:{PORT} (threaded)")
        httpd.serve_forever()

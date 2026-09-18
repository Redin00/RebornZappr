#!/usr/bin/env python3
"""
update_channels_from_iptv.py

Fetches an IPTV M3U playlist (e.g. SuperFranky84/IPTV-Italia or TVITALIA) and updates
channel streams, stream types, DRM licenses (ClearKey hex/JWK, Widevine), and EPG in
channels/it/dtt/national.json.

Usage:
    python3 scripts/update_channels_from_iptv.py [OPTIONS]

Options:
    --url URL           M3U playlist URL (default: SuperFranky84/IPTV-Italia TV)
    --file PATH         Local M3U playlist file instead of downloading
    --target PATH       Path to national.json (default: channels/it/dtt/national.json)
    --dry-run           Preview changes without modifying the target JSON file
    --check-streams     Verify streams are reachable (HTTP 200/302) concurrently before updating
    --timeout SECS      Timeout in seconds for stream checks (default: 4.0)
    --override-epg      Overwrite existing EPG with M3U tvg-id if present
    --add-new           Add new channels from M3U if not found in national.json
    --no-backup         Do not create a backup file before writing
    --verbose           Print verbose debug information
"""

import argparse
import base64
import concurrent.futures
import json
import os
import re
import shutil
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

DEFAULT_M3U_URL = "https://raw.githubusercontent.com/SuperFranky84/IPTV-Italia/main/TV"
DEFAULT_TARGET_JSON = os.path.join(PROJECT_ROOT, "channels", "it", "dtt", "national.json")

# Explicit channel mappings between M3U names/LCNs and national.json
KNOWN_MAPPINGS = {
    "rai3": {"name": "Rai 3", "lcn": 103},
    "k2": {"name": "K2", "lcn": 41},
    "frisbee": {"name": "Frisbee", "lcn": 44},
    "la7cinema": {"name": "LA7 Cinema", "lcn": 29},
    "la7d": {"name": "LA7 Cinema", "lcn": 29},
    "discoverychannel": {"name": "Discovery", "lcn": 37},
    "discovery": {"name": "Discovery", "lcn": 37},
    "discoveryturbo": {"name": "Discovery Turbo", "lcn": 59},
    "motortrend": {"name": "Discovery Turbo", "lcn": 59},
    "hgtvhomegarden": {"name": "HGTV - Home & Garden", "lcn": 56},
    "padrepiotv": {"name": "Padre Pio TV", "lcn": 145},
    "gamberorosso": {"name": "Gambero Rosso", "lcn": 257},
    "raisport": {"name": "Rai Sport", "lcn": 58},
    "raisporthd": {"name": "Rai Sport", "lcn": 58},
    "raisportplus": {"name": "Rai Sport", "lcn": 58},
    "raisportplushd": {"name": "Rai Sport", "lcn": 58},
    "twentyseven": {"name": "TwentySeven", "lcn": 27},
    "mediaset27twentyseven": {"name": "TwentySeven", "lcn": 27},
    "20mediaset": {"name": "20 Mediaset", "lcn": 20},
    "mediaset20": {"name": "20 Mediaset", "lcn": 20},
    "super": {"name": "Super!", "lcn": 47},
    "supersix": {"name": "SuperSix", "lcn": 833},
    "man-ga": {"name": "Man-ga", "lcn": 236},
    "manga": {"name": "Man-ga", "lcn": 236},
    "sportitalia": {"name": "Sportitalia", "lcn": 60},
    "sportitaliahd": {"name": "Sportitalia", "lcn": 60},
    "virginradio": {"name": "Virgin Radio", "lcn": 786},
    "radiofreccia": {"name": "Radiofreccia", "lcn": 738},
    "radiozeta": {"name": "Radio Zeta", "lcn": 737},
    "radiom2otv": {"name": "Radio m2o", "lcn": 715},
    "radiom2o": {"name": "Radio m2o", "lcn": 715},
    "m2otv": {"name": "Radio m2o", "lcn": 715},
    "radiocapitaltv": {"name": "Radio Capital", "lcn": 713},
    "radiocapital": {"name": "Radio Capital", "lcn": 713},
    "radiomontecarlo": {"name": "Radio Monte Carlo", "lcn": 772},
    "rmc": {"name": "Radio Monte Carlo", "lcn": 772},
    "kisskisstv": {"name": "Kiss Kiss TV", "lcn": 158},
    "radiokisskisstv": {"name": "Kiss Kiss TV", "lcn": 158},
    "rtl1025": {"name": "RTL 102.5", "lcn": 36},
    "rtl1025tv": {"name": "RTL 102.5", "lcn": 36},
    "rtl1025news": {"name": "RTL 102.5 Caliente", "lcn": 233},
    "rairadio2": {"name": "Rai Radio 2 Visual", "lcn": 202},
}


def normalize_str(s: str) -> str:
    """Strip punctuation, spaces, and lowercase for robust matching."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def b64url_to_hex(s: str) -> str:
    """Convert base64url string to hex string."""
    rem = len(s) % 4
    if rem > 0:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s).hex()


def parse_clearkey_value(val: str) -> str:
    """
    Parse ClearKey value from either:
    1. Hex string 'kid:key' (e.g. 'a03e33a2...:8f39a933...')
    2. JWK JSON format '{"keys":[{"kty":"oct","kid":"...","k":"..."}]}'
    Returns 'kid_hex:key_hex'.
    """
    val = val.strip()
    if val.startswith("{") and "keys" in val:
        try:
            jwk = json.loads(val)
            keys = jwk.get("keys", [])
            if keys and "kid" in keys[0] and "k" in keys[0]:
                kid_hex = b64url_to_hex(keys[0]["kid"])
                k_hex = b64url_to_hex(keys[0]["k"])
                return f"{kid_hex}:{k_hex}"
        except Exception:
            pass
    return val


def clean_github_url(url: str) -> str:
    """Convert a github.com URL to raw.githubusercontent.com if needed."""
    if "github.com" in url:
        if "/blob/" in url:
            return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        elif "/tree/main" in url:
            # e.g. https://github.com/SuperFranky84/IPTV-Italia/tree/main -> append TV
            base = url.replace("github.com", "raw.githubusercontent.com").replace("/tree/", "/")
            if not base.endswith("/TV"):
                base = base.rstrip("/") + "/TV"
            return base
    return url


def fetch_m3u_content(url_or_path: str, is_file: bool = False) -> str:
    """Fetch M3U content from URL or local file."""
    if is_file or os.path.exists(url_or_path):
        with open(url_or_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    url = clean_github_url(url_or_path)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_m3u(content: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Parse M3U content and return global header attributes and channel dicts."""
    header_attrs = {}
    channels = []
    lines = content.splitlines()

    curr_entry: Optional[Dict[str, Any]] = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTM3U"):
            tvg_url = re.search(r'x-tvg-url="([^"]+)"', line)
            if tvg_url:
                header_attrs["x-tvg-url"] = tvg_url.group(1)
            continue

        if line.startswith("##EXTINF"):
            curr_entry = None
            continue

        if line.startswith("#EXTINF:"):
            curr_entry = {
                "name": "",
                "lcn": None,
                "tvg_id": None,
                "tvg_logo": None,
                "group_title": None,
                "user_agent": None,
                "license_type": None,
                "license_key": None,
                "url": "",
            }

            chno_m = re.search(r'tvg-chno="([^"]+)"', line)
            if chno_m:
                try:
                    curr_entry["lcn"] = int(chno_m.group(1))
                except ValueError:
                    pass

            id_m = re.search(r'tvg-id="([^"]+)"', line)
            if id_m:
                curr_entry["tvg_id"] = id_m.group(1)

            logo_m = re.search(r'tvg-logo="([^"]+)"', line)
            if logo_m:
                curr_entry["tvg_logo"] = logo_m.group(1)

            group_m = re.search(r'group-title="([^"]+)"', line)
            if group_m:
                curr_entry["group_title"] = group_m.group(1)

            ua_m = re.search(r'http-user-agent="([^"]+)"', line)
            if ua_m:
                curr_entry["user_agent"] = ua_m.group(1).replace("http-user-agent=", "")

            if "," in line:
                curr_entry["name"] = line.rsplit(",", 1)[-1].strip()

            continue

        if curr_entry is not None:
            if line.startswith("#KODIPROP:inputstream.adaptive.license_type="):
                curr_entry["license_type"] = line.split("=", 1)[1].strip()
                continue
            elif line.startswith("#KODIPROP:inputstream.adaptive.license_key="):
                curr_entry["license_key"] = line.split("=", 1)[1].strip()
                continue
            elif line.startswith("#EXTVLCOPT:http-user-agent="):
                curr_entry["user_agent"] = line.split("=", 1)[1].strip()
                continue
            elif line.startswith("#"):
                continue
            elif line.startswith("http://") or line.startswith("https://"):
                curr_entry["url"] = line
                channels.append(curr_entry)
                curr_entry = None

    return header_attrs, channels


def detect_stream_type(url: str) -> str:
    """Detect stream type (hls or dash) from stream URL."""
    clean_url = url.split("?")[0].lower()
    if clean_url.endswith(".mpd"):
        return "dash"
    elif clean_url.endswith(".m3u8") or "relinkerservlet" in clean_url or "playlist" in clean_url:
        return "hls"
    return "hls"


def test_single_stream(item: Tuple[int, Dict[str, Any]], timeout: float = 4.0) -> Tuple[int, bool, int, str]:
    """Test a single stream URL."""
    idx, m3u_ch = item
    url = m3u_ch.get("url", "")
    user_agent = m3u_ch.get("user_agent") or "HbbTV/1.6.1; Mozilla/5.0"
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*"
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            return idx, (code in (200, 206, 301, 302, 307, 308)), code, "OK"
    except urllib.error.HTTPError as e:
        return idx, (e.code in (200, 206, 301, 302, 307, 308)), e.code, str(e.reason)
    except Exception as e:
        return idx, False, 0, str(e)


def find_channel_match(
    m3u_ch: Dict[str, Any],
    json_channels_by_lcn: Dict[int, Dict[str, Any]],
    json_channels_by_norm_name: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Match an M3U channel to an existing national.json channel."""
    m_name = m3u_ch.get("name", "")
    m_lcn = m3u_ch.get("lcn")
    norm_m_name = normalize_str(m_name)

    # 1. Known explicit mappings
    if norm_m_name in KNOWN_MAPPINGS:
        target = KNOWN_MAPPINGS[norm_m_name]
        if target["lcn"] in json_channels_by_lcn:
            return json_channels_by_lcn[target["lcn"]]

    # 2. Exact normalized name
    if norm_m_name in json_channels_by_norm_name:
        return json_channels_by_norm_name[norm_m_name]

    # 3. Match by LCN if names are compatible
    if m_lcn is not None and m_lcn in json_channels_by_lcn:
        cand = json_channels_by_lcn[m_lcn]
        cand_norm = normalize_str(cand.get("name", ""))
        if norm_m_name == cand_norm or norm_m_name in cand_norm or cand_norm in norm_m_name:
            return cand

    # 4. Partial word match
    for cand_norm, cand in json_channels_by_norm_name.items():
        if len(norm_m_name) >= 4 and len(cand_norm) >= 4:
            if norm_m_name in cand_norm or cand_norm in norm_m_name:
                return cand

    return None


def update_channel_data(
    target_ch: Dict[str, Any],
    m3u_ch: Dict[str, Any],
    override_epg: bool = False
) -> Dict[str, Any]:
    """Apply updates from M3U channel to target channel dictionary."""
    changes = {}
    new_url = m3u_ch["url"]
    new_type = detect_stream_type(new_url)

    # Update URL
    old_url = target_ch.get("url")
    if old_url != new_url:
        changes["url"] = (old_url, new_url)
        target_ch["url"] = new_url

    # Update Type (dash / hls)
    old_type = target_ch.get("type")
    if old_type != new_type:
        changes["type"] = (old_type, new_type)
        target_ch["type"] = new_type

    # Update HTTP flag
    is_http = new_url.startswith("http://")
    if is_http:
        if not target_ch.get("http"):
            changes["http"] = (target_ch.get("http"), True)
            target_ch["http"] = True
    else:
        if "http" in target_ch:
            changes["http"] = (target_ch.get("http"), None)
            del target_ch["http"]

    # License details handling
    m_lic_type = m3u_ch.get("license_type")
    m_lic_key = m3u_ch.get("license_key")

    if m_lic_type:
        if "widevine" in m_lic_type.lower():
            old_lic = target_ch.get("license")
            old_lic_det = target_ch.get("licensedetails")
            new_lic = "widevine"
            new_lic_det = {"serverURL": m_lic_key} if m_lic_key else {}
            if old_lic != new_lic or old_lic_det != new_lic_det:
                changes["license"] = (old_lic, new_lic)
                changes["licensedetails"] = (old_lic_det, new_lic_det)
                target_ch["license"] = new_lic
                target_ch["licensedetails"] = new_lic_det
        elif "clearkey" in m_lic_type.lower():
            old_lic = target_ch.get("license")
            old_lic_det = target_ch.get("licensedetails")
            new_lic = "clearkey"
            new_lic_det = parse_clearkey_value(m_lic_key) if m_lic_key else ""
            if old_lic != new_lic or old_lic_det != new_lic_det:
                changes["license"] = (old_lic, new_lic)
                changes["licensedetails"] = (old_lic_det, new_lic_det)
                target_ch["license"] = new_lic
                target_ch["licensedetails"] = new_lic_det
    else:
        # Clear unencrypted stream: remove stale DRM on main stream
        if ("-clr" in new_url.lower() or "aurora" in new_url.lower()) and "license" in target_ch:
            changes["license"] = (target_ch.get("license"), None)
            del target_ch["license"]
            if "licensedetails" in target_ch:
                changes["licensedetails"] = (target_ch.get("licensedetails"), None)
                del target_ch["licensedetails"]

    # EPG Handling
    tvg_id = m3u_ch.get("tvg_id")
    if tvg_id:
        if "epg" not in target_ch or override_epg:
            new_epg = {"source": "epgshare", "id": tvg_id}
            if target_ch.get("epg") != new_epg:
                changes["epg"] = (target_ch.get("epg"), new_epg)
                target_ch["epg"] = new_epg

    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Update channels in channels/it/dtt/national.json from an IPTV M3U playlist."
    )
    parser.add_argument("--url", default=DEFAULT_M3U_URL, help=f"M3U playlist URL (default: {DEFAULT_M3U_URL})")
    parser.add_argument("--file", help="Local M3U playlist file to read instead of fetching via URL")
    parser.add_argument("--target", default=DEFAULT_TARGET_JSON, help=f"Target JSON file (default: {DEFAULT_TARGET_JSON})")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying the target JSON")
    parser.add_argument("--check-streams", action="store_true", help="Verify stream URLs respond with HTTP 200/302 before applying")
    parser.add_argument("--timeout", type=float, default=4.0, help="Stream verification timeout in seconds (default: 4.0)")
    parser.add_argument("--override-epg", action="store_true", help="Override existing EPG with M3U tvg-id")
    parser.add_argument("--add-new", action="store_true", help="Add new channels found in M3U that do not exist in national.json")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a backup file before saving")
    parser.add_argument("--verbose", action="store_true", help="Print detailed logs")

    args = parser.parse_args()

    # 1. Load target national.json
    target_path = args.target
    if not os.path.exists(target_path):
        alt_path = os.path.join(PROJECT_ROOT, target_path)
        if os.path.exists(alt_path):
            target_path = alt_path
        else:
            print(f"Error: Target file not found: {args.target} (also checked {alt_path})", file=sys.stderr)
            sys.exit(1)

    with open(target_path, "r", encoding="utf-8") as f:
        national_data = json.load(f)

    json_channels = national_data.get("channels", [])
    print(f"Loaded {len(json_channels)} entries from {target_path}")

    by_lcn: Dict[int, Dict[str, Any]] = {}
    by_norm_name: Dict[str, Dict[str, Any]] = {}
    for ch in json_channels:
        if "lcn" in ch:
            by_lcn[ch["lcn"]] = ch
        if "name" in ch:
            by_norm_name[normalize_str(ch["name"])] = ch

    # 2. Fetch & Parse M3U
    m3u_source = args.file if args.file else args.url
    print(f"Fetching M3U from: {m3u_source} ...")
    try:
        m3u_text = fetch_m3u_content(m3u_source, is_file=bool(args.file))
    except Exception as e:
        print(f"Error fetching M3U: {e}", file=sys.stderr)
        sys.exit(1)

    header_attrs, m3u_channels = parse_m3u(m3u_text)
    print(f"Parsed {len(m3u_channels)} valid channels from M3U playlist.")
    if header_attrs.get("x-tvg-url"):
        print(f"Found EPG TVG URL in M3U: {header_attrs['x-tvg-url']}")

    # 3. Optional concurrent stream health check
    stream_status: Dict[int, Tuple[bool, int, str]] = {}
    if args.check_streams:
        socket.setdefaulttimeout(args.timeout)
        print(f"\nChecking stream availability concurrently ({len(m3u_channels)} streams, timeout {args.timeout}s)...")
        tasks = list(enumerate(m3u_channels))
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(test_single_stream, task, args.timeout) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                idx, ok, code, reason = future.result()
                stream_status[idx] = (ok, code, reason)
        live_count = sum(1 for ok, _, _ in stream_status.values() if ok)
        dead_count = len(stream_status) - live_count
        print(f"Stream check complete: {live_count} live, {dead_count} unresponsive/skipped.")

    # 4. Process each M3U channel
    updated_count = 0
    unchanged_count = 0
    skipped_dead_count = 0
    unmatched_channels = []

    print("\nProcessing channels...")

    for idx, m3u_ch in enumerate(m3u_channels):
        m_name = m3u_ch.get("name", "Unknown")
        m_lcn = m3u_ch.get("lcn")
        m_url = m3u_ch.get("url")

        target_ch = find_channel_match(m3u_ch, by_lcn, by_norm_name)

        if not target_ch:
            unmatched_channels.append(m3u_ch)
            if args.verbose:
                print(f"  [UNMATCHED] [{m_lcn}] {m_name}")
            continue

        if args.check_streams:
            ok, status, reason = stream_status.get(idx, (False, 0, "Not checked"))
            if not ok:
                skipped_dead_count += 1
                print(f"  [STREAM DOWN] [{target_ch.get('lcn')}] {target_ch.get('name')}: {m_url} (HTTP {status} - {reason}) -> SKIPPED")
                continue

        changes = update_channel_data(target_ch, m3u_ch, override_epg=args.override_epg)

        if changes:
            updated_count += 1
            print(f"  [UPDATED] LCN {target_ch.get('lcn')}: {target_ch.get('name')}")
            for field, (old_val, new_val) in changes.items():
                if field == "url":
                    print(f"    - url: {old_val[:60] if old_val else 'None'}... -> {new_val[:60]}...")
                elif field in ("license", "licensedetails"):
                    print(f"    - {field}: {old_val} -> {new_val}")
                else:
                    print(f"    - {field}: {old_val} -> {new_val}")
        else:
            unchanged_count += 1
            if args.verbose:
                print(f"  [OK] LCN {target_ch.get('lcn')}: {target_ch.get('name')} (already up-to-date)")

    # 5. Handle unmatched channels if --add-new
    added_count = 0
    if args.add_new and unmatched_channels:
        print(f"\nAdding {len(unmatched_channels)} new channels from M3U...")
        for m_ch in unmatched_channels:
            if not m_ch.get("name") or not m_ch.get("url"):
                continue

            new_entry = {
                "name": m_ch["name"],
                "type": detect_stream_type(m_ch["url"]),
                "url": m_ch["url"],
            }
            if m_ch.get("lcn"):
                new_entry["lcn"] = m_ch["lcn"]
            if m_ch["url"].startswith("http://"):
                new_entry["http"] = True
            if m_ch.get("license_type"):
                if "widevine" in m_ch["license_type"].lower():
                    new_entry["license"] = "widevine"
                    new_entry["licensedetails"] = {"serverURL": m_ch.get("license_key", "")}
                elif "clearkey" in m_ch["license_type"].lower():
                    new_entry["license"] = "clearkey"
                    new_entry["licensedetails"] = parse_clearkey_value(m_ch.get("license_key", ""))
            if m_ch.get("tvg_id"):
                new_entry["epg"] = {"source": "epgshare", "id": m_ch["tvg_id"]}

            json_channels.append(new_entry)
            added_count += 1
            print(f"  [NEW] [{new_entry.get('lcn', '-')}] {new_entry['name']}")

    # 6. Save changes
    print("\n" + "=" * 50)
    print(f"Summary:")
    print(f"  - Channels updated:     {updated_count}")
    print(f"  - Channels unchanged:   {unchanged_count}")
    if args.check_streams:
        print(f"  - Streams down/skipped: {skipped_dead_count}")
    print(f"  - Unmatched in M3U:     {len(unmatched_channels)}")
    if args.add_new:
        print(f"  - New channels added:   {added_count}")
    print("=" * 50)

    if not args.dry_run and (updated_count > 0 or added_count > 0):
        if not args.no_backup:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{target_path}.bak.{ts}"
            shutil.copy2(target_path, backup_path)
            print(f"Backup saved to: {backup_path}")

        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(national_data, f, indent=4, ensure_ascii=False)
            f.write("\n")
        print(f"Successfully updated {target_path}!")
    elif args.dry_run:
        print("Dry run completed. No files were modified.")
    else:
        print("No changes required. File is already up to date.")


if __name__ == "__main__":
    main()

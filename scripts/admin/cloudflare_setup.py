"""Idempotent Cloudflare setup for the site: the Pages project, its custom domain and the DNS record.

Needs CLOUDFLARE_API_TOKEN (Pages edit, Zone read, DNS edit on jamesconway.dev) and CLOUDFLARE_ACCOUNT_ID.
Touches no DNS record other than devstream.jamesconway.dev.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
PROJECT = "devstream"
ZONE = "jamesconway.dev"
HOST = f"{PROJECT}.{ZONE}"
TARGET = f"{PROJECT}.pages.dev"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status


def http_api(token):
    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as err:
            raise ApiError(err.code, err.read().decode(errors="replace")) from None
        # Cloudflare can answer 200 with success false; that is still a failure.
        if body.get("success") is not True:
            raise ApiError(200, json.dumps(body.get("errors")))
        return body["result"]

    return call


def run(api, account):
    base = f"/accounts/{account}/pages/projects"
    try:
        api("GET", f"{base}/{PROJECT}")
        print(f"Pages project {PROJECT} exists")
    except ApiError as err:
        if err.status != 404:
            raise
        api("POST", base, {"name": PROJECT, "production_branch": "main"})
        print(f"Created Pages project {PROJECT}")

    if any(d["name"] == HOST for d in api("GET", f"{base}/{PROJECT}/domains")):
        print(f"Custom domain {HOST} is attached")
    else:
        api("POST", f"{base}/{PROJECT}/domains", {"name": HOST})
        print(f"Attached custom domain {HOST}")

    zones = api("GET", f"/zones?name={ZONE}")
    if not zones:
        sys.exit(f"Zone {ZONE} not found for this token")
    zone = zones[0]["id"]
    records = api("GET", f"/zones/{zone}/dns_records?name={HOST}")
    if not records:
        api("POST", f"/zones/{zone}/dns_records", {"type": "CNAME", "name": PROJECT, "content": TARGET, "proxied": True, "ttl": 1})
        print(f"Created CNAME {HOST} -> {TARGET}")
        return
    rec = records[0]
    if rec["type"] != "CNAME":
        # Someone pointed this name somewhere else on purpose; replacing it is a human call.
        sys.exit(f"{HOST} already has a {rec['type']} record; remove it by hand first")
    if rec["content"] != TARGET or not rec["proxied"]:
        api("PATCH", f"/zones/{zone}/dns_records/{rec['id']}", {"content": TARGET, "proxied": True})
        print(f"Updated CNAME {HOST} -> {TARGET}")
    else:
        print(f"CNAME {HOST} -> {TARGET} is in place")


def main():
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        sys.exit("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
    run(http_api(token), account)


if __name__ == "__main__":
    main()

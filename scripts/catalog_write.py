#!/usr/bin/env python3
"""Tulis satu entri judul ke catalog.json repo ini.

Dipakai workflow Publish Catalog (GitHub Actions) sebagai pengganti tulis
katalog lewat surrogate (ambang ukuran permintaan surrogate kini di bawah
ukuran katalog). Entri diterima lewat env ENTRY_JSON (JSON minified, dibuat
di sandbox oleh title_pipeline.append_catalog — semua metadata/poster/banner
sudah lengkap di sana; runner hanya melakukan append mekanis).

Metode:
  git : checkout sudah dilakukan workflow; skrip append + commit + push.
        Push dari shallow clone (fetch-depth 1) diuji lolos.
  api : skrip memakai Git Data API langsung (GITHUB_TOKEN) — tanpa git.
        Cermin logika _write_catalog_locked di title_pipeline.py.

Semantik upsert SAMA dengan _write_catalog_locked:
  - judul sudah ada -> update episodes/repo/pin/poster/banner/synopsis/
    year/score/genres/status; poster/banner self-hosted yang sudah ada
    (cdn.jsdelivr.net/gh/adiytharpansa/) TIDAK ditimpa URL luar.
  - judul baru -> append.
  - berkas ditulis JSON rapat/minified (separators (',', ':')).
  - pesan commit: "Katalog: tambah <title> (<n> episode)".
Keluar 0 bila entri sudah ada di katalog dengan isi persis sama (idempotent),
atau sesudah commit+push berhasil. Keluar 1 bila gagal sesudah retry.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CATALOG = 'catalog.json'


def upsert(catalog, entry):
    existing = next((t for t in catalog['titles'] if t['id'] == entry['id']), None)
    if existing is not None:
        for k in ('poster', 'banner'):
            old = existing.get(k) or ''
            if 'cdn.jsdelivr.net/gh/adiytharpansa/' in old:
                entry[k] = old
        existing.update({k: entry[k] for k in (
            'episodes', 'repo', 'pin', 'poster', 'banner',
            'synopsis', 'year', 'score', 'genres', 'status') if k in entry})
    else:
        catalog['titles'].append(entry)
    return catalog


def dump(catalog):
    return json.dumps(catalog, ensure_ascii=False, separators=(',', ':')).encode()


def git_cmd(*args):
    r = subprocess.run(['git'] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} gagal: {r.stderr.strip()[:300]}")
    return r


def method_git(entry):
    git_cmd('config', 'user.name', 'github-actions[bot]')
    git_cmd('config', 'user.email', 'github-actions[bot]@users.noreply.github.com')
    msg = f"Katalog: tambah {entry['title']} ({len(entry['episodes'])} episode)"
    for attempt in range(3):
        with open(CATALOG, 'rb') as f:
            catalog = json.load(f)
        upsert(catalog, entry)
        with open(CATALOG, 'wb') as f:
            f.write(dump(catalog))
        git_cmd('add', CATALOG)
        r = subprocess.run(['git', 'diff', '--cached', '--quiet'])
        if r.returncode == 0:
            print('tidak ada perubahan (entri sudah sama persis)')
            return 0
        git_cmd('commit', '-m', msg)
        r = subprocess.run(['git', 'push', 'origin', 'main'],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print('push berhasil')
            return 0
        print(f"push gagal (percobaan {attempt + 1}): {r.stderr.strip()[:200]}")
        git_cmd('fetch', 'origin', 'main')
        git_cmd('reset', '--hard', 'origin/main')
    print('push gagal sesudah 3 percobaan')
    return 1


def api_req(method, path, payload=None, token=None, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request('https://api.github.com' + path, data=data, method=method)
    r.add_header('Accept', 'application/vnd.github.raw' if raw
                 else 'application/vnd.github+json')
    r.add_header('X-GitHub-Api-Version', '2022-11-28')
    r.add_header('Authorization', f'Bearer {token}')
    if data:
        r.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(r, timeout=120) as resp:
        body = resp.read()
        return resp.status, (body if raw else (json.loads(body.decode()) if body else {}))


def method_api(entry):
    token = os.environ['GITHUB_TOKEN']
    repo = os.environ['GITHUB_REPOSITORY']
    msg = f"Katalog: tambah {entry['title']} ({len(entry['episodes'])} episode)"
    for attempt in range(3):
        s, ref = api_req('GET', f'/repos/{repo}/git/ref/heads/main', token=token)
        parent = ref['object']['sha']
        s, commit = api_req('GET', f'/repos/{repo}/git/commits/{parent}', token=token)
        base_tree = commit['tree']['sha']
        s, tree = api_req('GET', f'/repos/{repo}/git/trees/{base_tree}', token=token)
        ent = next(t for t in tree['tree'] if t['path'] == CATALOG)
        # GET blob mentah (raw): GET JSON mengembalikan content kosong >1 MB.
        s, data = api_req('GET', f'/repos/{repo}/git/blobs/{ent["sha"]}',
                          token=token, raw=True)
        catalog = json.loads(data.decode())
        upsert(catalog, entry)
        newcat = dump(catalog)
        s, blob = api_req('POST', f'/repos/{repo}/git/blobs',
                          {'content': base64.b64encode(newcat).decode(),
                           'encoding': 'base64'}, token)
        if s not in (200, 201):
            print(f'blob gagal {s} (percobaan {attempt + 1})')
            time.sleep(5)
            continue
        s, ntree = api_req('POST', f'/repos/{repo}/git/trees',
                           {'base_tree': base_tree, 'tree': [
                               {'path': CATALOG, 'mode': '100644',
                                'type': 'blob', 'sha': blob['sha']}]}, token)
        s, ncommit = api_req('POST', f'/repos/{repo}/git/commits',
                             {'message': msg, 'tree': ntree['sha'],
                              'parents': [parent]}, token)
        s, upd = api_req('PATCH', f'/repos/{repo}/git/refs/heads/main',
                         {'sha': ncommit['sha']}, token)
        if s == 200:
            print('terbit:', ncommit['sha'])
            return 0
        print(f'ref ditolak (percobaan {attempt + 1}), coba lagi')
        time.sleep(5)
    print('gagal sesudah 3 percobaan')
    return 1


def main():
    method = sys.argv[sys.argv.index('--method') + 1] if '--method' in sys.argv else 'git'
    entry = json.loads(os.environ['ENTRY_JSON'])
    if method == 'git':
        return method_git(entry)
    if method == 'api':
        return method_api(entry)
    print(f'metode tidak dikenal: {method}')
    return 2


if __name__ == '__main__':
    sys.exit(main())

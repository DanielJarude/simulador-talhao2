"""Regression checks for authenticated texture/heightmap URL handling."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "index.html"


def test_frontend_authenticated_asset_flow_and_url_classification():
    """Exercise the actual browser helper with Node's built-in URL parser."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js não disponível para o teste do helper frontend")

    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index("function isAuthenticatedAssetUrl(url)")
    end = source.index("\n\n    const matReal", start)
    helper_and_loader = source[start:end]

    script = r"""
const vm = require('vm');
const assert = require('node:assert/strict');
const source = process.env.FRONTEND_HELPER;
const fetchCalls = [];
const loadedUrls = [];
const revokedUrls = [];
const loggedOut = [];
let responseStatus = 200;
let objectUrlCounter = 0;

class BrowserURL extends URL {}
BrowserURL.createObjectURL = () => `blob:mock-${++objectUrlCounter}`;
BrowserURL.revokeObjectURL = (url) => revokedUrls.push(url);

const sandbox = {
  URL: BrowserURL,
  Blob,
  window: { location: { href: 'https://frontend.example/dashboard.html' } },
  console: { warn() {} },
  THREE: { LinearMipmapLinearFilter: 'mipmap', LinearFilter: 'linear' },
  MAX_CACHED_TEXTURES: 8,
  textureCache: new Map(),
  textureInFlight: new Map(),
  textureLoader: {
    load(url, onLoad) {
      loadedUrls.push(url);
      queueMicrotask(() => onLoad({}));
    },
  },
  localStorage: { getItem: () => 'jwt-test-token' },
  AuthService: { logout: () => loggedOut.push(true) },
  fetch: async (url, options) => {
    fetchCalls.push({ url, options });
    return {
      status: responseStatus,
      ok: responseStatus >= 200 && responseStatus < 300,
      blob: async () => new Blob(['png-bytes'], { type: 'image/png' }),
    };
  },
};
vm.runInNewContext(`${source}; globalThis.classify = isAuthenticatedAssetUrl; globalThis.loadTexture = getCachedTexture;`, sandbox);

const classifications = {
  relative_texture: sandbox.classify('/api/talhao/2/texture.png'),
  absolute_texture: sandbox.classify('http://localhost:8000/api/talhao/2/texture.png?layer=ndvi'),
  absolute_heightmap: sandbox.classify('https://api.example.test/api/talhao/2/heightmap.png?size=256'),
  sentinel_asset: sandbox.classify('sentinel-21KXQ-2025-04-07/ndvi_cloudless_min_max.png'),
  static_asset: sandbox.classify('/assets/qualquer.png'),
};

const main = async () => {
  await sandbox.loadTexture('texture', 'http://localhost:8000/api/talhao/2/texture.png?layer=ndvi');
  await sandbox.loadTexture('heightmap', 'https://api.example.test/api/talhao/2/heightmap.png?size=256');
  assert.deepEqual(fetchCalls.map(({ options }) => options.headers.Authorization), [
    'Bearer jwt-test-token',
    'Bearer jwt-test-token',
  ]);
  assert.equal(loadedUrls.length, 2);
  assert.equal(revokedUrls.length, 2);

  const statusErrors = {};
  for (const status of [401, 403, 404]) {
    responseStatus = status;
    try {
      await sandbox.loadTexture(`error-${status}`, '/api/talhao/2/texture.png');
      statusErrors[status] = false;
    } catch (error) {
      statusErrors[status] = error.message;
    }
  }

  process.stdout.write(JSON.stringify({ classifications, statusErrors, loggedOut }));
};
main().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=REPO_ROOT,
        env={"FRONTEND_HELPER": helper_and_loader},
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["classifications"] == {
        "relative_texture": True,
        "absolute_texture": True,
        "absolute_heightmap": True,
        "sentinel_asset": False,
        "static_asset": False,
    }
    assert payload["statusErrors"] == {
        "401": "Sessão expirada.",
        "403": "Sem permissão para esta textura.",
        "404": "Textura não encontrada.",
    }
    assert payload["loggedOut"] == [True]

    # The blob URL must remain alive until TextureLoader finishes, then be
    # revoked on either the success or error callback.
    assert "URL.revokeObjectURL(blobUrl)" in source
    assert source.index("loadViaLoader(blobUrl,") < source.index("URL.revokeObjectURL(blobUrl)")

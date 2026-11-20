from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from meldfabric import AgentKit


@pytest.mark.parametrize("prefix", ["/admin", "/control"])
@pytest.mark.asyncio
async def test_admin_spa_routes_and_assets_preserve_api_boundaries(tmp_path, prefix):
    kit = AgentKit(state_dir=tmp_path)
    loaded = await kit.apatch_config({"runtime": {"management": {"path": prefix}}})
    await kit.aapply_latest_config(expected_generation=loaded.config.config_generation)
    with TestClient(kit.create_app()) as client:
        for suffix in ("", "/ui/", "/ui/agents/missing/prompt", "/ui/resources/databases"):
            page = client.get(prefix + suffix)
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert '<div id="app"></div>' in page.text
            assert 'content="' + prefix + '"' in page.text
        asset_urls = re.findall(r'(?:src|href)="([^"]+/assets/[^"]+)"', page.text)
        assert len(asset_urls) >= 2
        for url in asset_urls:
            asset = client.get(url)
            assert asset.status_code == 200
            assert "immutable" in asset.headers["cache-control"]
            assert "html" not in asset.headers["content-type"]
        assert client.get(prefix + "/static/assets/missing.js").status_code == 404
        assert client.get(prefix + "/static/build.json").status_code == 404
        assert client.get(prefix + "/static/assets/%2e%2e/%2e%2e/api.py").status_code == 404
        assert client.get(prefix + "/config").status_code == 401
        assert client.get(prefix + "/unknown-api").status_code == 404
        if prefix != "/admin":
            assert client.get("/admin/ui/").status_code == 404


def test_management_assets_are_always_part_of_the_service(tmp_path):
    kit = AgentKit(state_dir=tmp_path)
    with TestClient(kit.create_app()) as client:
        assert client.get("/admin/ui/").status_code == 200
        assert client.get("/admin/static/assets/missing.js").status_code == 404


def test_spa_uses_proxy_root_path(tmp_path):
    kit = AgentKit(state_dir=tmp_path)
    with TestClient(kit.create_app(), root_path="/prefix") as client:
        page = client.get("/admin/ui/agents")
        assert 'content="/prefix/admin"' in page.text
        assert 'content="/prefix"' in page.text
        assert "/prefix/admin/static/assets/" in page.text

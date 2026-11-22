from __future__ import annotations

from tag_manager_mcp.diff import diff_versions


def test_diff_versions_added_removed_changed_and_ignored_keys() -> None:
    old = {
        "containerVersionId": "1",
        "tag": [
            {
                "tagId": "1",
                "name": "Unchanged Except Meta",
                "type": "gaawe",
                "fingerprint": "fp-old",
                "accountId": "10",
                "containerId": "20",
                "path": "accounts/10/containers/20/versions/1/tags/1",
                "tagManagerUrl": "https://tagmanager.google.com/old",
                "workspaceId": "0",
                "parentFolderId": "5",
            },
            {
                "tagId": "2",
                "name": "Modified Tag",
                "type": "html",
                "paused": False,
                "parentFolderId": "5",
                "fingerprint": "fp-2-old",
            },
            {
                "tagId": "3",
                "name": "Removed Tag",
                "type": "img",
            },
        ],
        "trigger": [
            {"triggerId": "10", "name": "All Pages", "type": "pageview"},
        ],
        "builtInVariable": [
            {"type": "pageUrl", "name": "Page URL"},
            {"type": "pagePath", "name": "Page Path"},
        ],
    }

    new = {
        "containerVersionId": "2",
        "tag": [
            {
                "tagId": "1",
                "name": "Unchanged Except Meta",
                "type": "gaawe",
                "fingerprint": "fp-new",
                "accountId": "99",
                "containerId": "88",
                "path": "accounts/99/containers/88/versions/2/tags/1",
                "tagManagerUrl": "https://tagmanager.google.com/new",
                "workspaceId": "4",
                "parentFolderId": "5",
            },
            {
                "tagId": "2",
                "name": "Modified Tag",
                "type": "html",
                "paused": True,
                "parentFolderId": "6",
                "fingerprint": "fp-2-new",
            },
            {
                "tagId": "4",
                "name": "Added Tag",
                "type": "gaawe",
            },
        ],
        "trigger": [
            {"triggerId": "10", "name": "All Pages", "type": "pageview"},
        ],
        "builtInVariable": [
            {"type": "pageUrl", "name": "Page URL"},
            {"type": "referrer", "name": "Referrer"},
        ],
    }

    result = diff_versions(old, new)

    assert result["from"] == "1"
    assert result["to"] == "2"
    assert result["changes"]["tag"] == {
        "added": [{"id": "4", "name": "Added Tag", "fields": []}],
        "removed": [{"id": "3", "name": "Removed Tag", "fields": []}],
        "changed": [
            {
                "id": "2",
                "name": "Modified Tag",
                "fields": ["parentFolderId", "paused"],
            }
        ],
    }
    assert result["changes"]["trigger"] == {"added": [], "removed": [], "changed": []}
    assert result["changes"]["builtInVariable"] == {
        "added": [{"id": "referrer", "name": "Referrer", "fields": []}],
        "removed": [{"id": "pagePath", "name": "Page Path", "fields": []}],
        "changed": [],
    }
    assert result["total"] == 5


def test_diff_versions_deterministic_sorting_by_name_then_id() -> None:
    old = {"containerVersionId": "10", "variable": []}
    new = {
        "containerVersionId": "11",
        "variable": [
            {"variableId": "9", "name": "Zeta"},
            {"variableId": "2", "name": "Alpha"},
            {"variableId": "1", "name": "Alpha"},
        ],
    }

    result = diff_versions(old, new)
    assert result["changes"]["variable"]["added"] == [
        {"id": "1", "name": "Alpha", "fields": []},
        {"id": "2", "name": "Alpha", "fields": []},
        {"id": "9", "name": "Zeta", "fields": []},
    ]
    assert result["total"] == 3


def test_diff_versions_includes_gtag_config() -> None:
    old = {
        "containerVersionId": "1",
        "gtagConfig": [
            {
                "gtagConfigId": "10",
                "type": "googtag",
                "parameter": [{"key": "tagId", "value": "G-1"}],
            },
            {
                "gtagConfigId": "20",
                "type": "googtag",
                "parameter": [{"key": "tagId", "value": "G-2"}],
            },
        ],
    }
    new = {
        "containerVersionId": "2",
        "gtagConfig": [
            {
                "gtagConfigId": "10",
                "type": "googtag",
                "parameter": [{"key": "tagId", "value": "G-1B"}],
            },
            {
                "gtagConfigId": "30",
                "type": "googtag",
                "parameter": [{"key": "tagId", "value": "G-3"}],
            },
        ],
    }

    result = diff_versions(old, new)
    assert result["changes"]["gtagConfig"] == {
        "added": [{"id": "30", "name": "", "fields": []}],
        "removed": [{"id": "20", "name": "", "fields": []}],
        "changed": [{"id": "10", "name": "", "fields": ["parameter"]}],
    }
    assert result["total"] == 3

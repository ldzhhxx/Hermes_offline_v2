import pathlib

REPO = pathlib.Path(__file__).parent.parent
WORKSPACE_JS = (REPO / 'static' / 'workspace.js').read_text(encoding='utf-8')
UI_JS = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')


def test_workspace_js_auto_creates_browse_session_for_profile_workspace():
    assert 'async function _ensureWorkspaceBrowseSession()' in WORKSPACE_JS
    assert "api('/api/session/new'" in WORKSPACE_JS
    assert "JSON.stringify({workspace:ws})" in WORKSPACE_JS


def test_load_dir_and_open_file_recover_from_sessionless_workspace_boot():
    assert "if(!S.session&&!(await _ensureWorkspaceBrowseSession()))return;" in WORKSPACE_JS
    assert WORKSPACE_JS.count("if(!S.session&&!(await _ensureWorkspaceBrowseSession()))return;") >= 2


def test_render_file_tree_honors_profile_default_workspace_without_active_session():
    assert "const fallbackWorkspace=!!((!S.session)&&S._profileDefaultWorkspace);" in UI_JS
    assert "const hasWorkspace=!!((S.session&&S.session.workspace)||fallbackWorkspace);" in UI_JS

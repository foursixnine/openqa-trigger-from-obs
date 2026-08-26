import pytest

from script.scriptgen import ActionGenerator


def make_generator(
    project: str, productpath: str, rsync_user: str = ""
) -> ActionGenerator:
    return ActionGenerator(
        envdir="/tmp",
        project=project,
        productpath=productpath,
        version="1.0",
        brand="obs",
        rsync_user=rsync_user,
    )


@pytest.mark.parametrize(
    "project,productpath,rsync_user,expected",
    [
        pytest.param(
            "openSUSE:Factory:ToTest",
            "",
            "",
            "obspublish::openqa/openSUSE:Factory:ToTest",
            id="default_rsync_host_without_user_for_non_staging_project",
        ),
        pytest.param(
            "openSUSE:Factory:Staging:B",
            "",
            "geekotest",
            "geekotest@obspublish-stage::openqa/openSUSE:Factory:Staging:B",
            id="default_rsync_host_with_user_for_staging_project",
        ),
        pytest.param(
            "systemsmanagement:Agama:Devel",
            "obspublish-other::openqa/systemsmanagement:Agama:Devel/",
            "geekotest",
            "geekotest@obspublish-other::openqa/systemsmanagement:Agama:Devel/",
            id="prefixes_user_on_explicit_rsync_resource",
        ),
        pytest.param(
            "some:Project",
            "https://example.invalid/path",
            "geekotest",
            "https://example.invalid/path",
            id="leaves_http_productpath_unchanged",
        ),
        pytest.param(
            "some:Project",
            "existing@obspublish-other::openqa/some:Project/",
            "geekotest",
            "existing@obspublish-other::openqa/some:Project/",
            id="does_not_double_prefix_existing_rsync_user",
        ),
    ],
)
def test_init_productpath(project, productpath, rsync_user, expected):
    ag = make_generator(project, productpath, rsync_user=rsync_user)
    assert ag.productpath == expected

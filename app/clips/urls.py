from django.urls import path, register_converter
from django.views.generic import RedirectView

from . import views


class CodeConverter:
    """Matches a clip's short public code (base62, 7 chars) — narrow enough that a bare /<code>
    route can't swallow real top-level paths (they have dots/slashes or a different length)."""
    regex = r"[0-9A-Za-z]{7}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(CodeConverter, "code")

urlpatterns = [
    # Search IS the root (the discovery front door) — not a redirect.
    path("", views.search_page, name="clips_search"),

    # SEO endpoints (root-level by convention; the routes below only match dot-free short codes).
    path("robots.txt", views.robots_txt, name="clips_robots"),
    path("sitemap.xml", views.sitemap_xml, name="clips_sitemap"),

    # Canonical root URLs for a clip — short + shareable (clip.cool/<code>). One page per clip:
    # humans get the full page, chat/social unfurl off its OG/Twitter meta. .gif/.mp4 are direct
    # rendition links. The UUID pk still backs internal lookups; only the public URL uses the code.
    path("<code:asset_id>", views.asset_detail, name="clips_asset"),
    path("<code:asset_id>.gif", views.public_clip_gif, name="clip_public_gif"),
    path("<code:asset_id>.mp4", views.public_clip_mp4, name="clip_public_mp4"),
    path("<code:asset_id>/download", views.clip_download, name="clip_download"),
    path("<code:asset_id>/download.gif", views.clip_download_gif, name="clip_download_gif"),
    # Keyword-rich alias: /<code>/<slug-from-OCR-text>. Same page as the bare /<code> (the code
    # resolves it); the slug is what Google indexes. MUST come after /download so that isn't read
    # as a slug. A wrong/stale slug 301s to the canonical one in the view.
    path("<code:asset_id>/<slug:slug>", views.asset_detail, name="clips_asset_slug"),

    # 301 the old full-UUID paths (already shared / indexed) to the short code URL.
    path("<uuid:asset_id>", views.legacy_redirect, {"to": "clips_asset"}),
    path("<uuid:asset_id>.gif", views.legacy_redirect, {"to": "clip_public_gif"}),
    path("<uuid:asset_id>.mp4", views.legacy_redirect, {"to": "clip_public_mp4"}),
    path("<uuid:asset_id>/download", views.legacy_redirect, {"to": "clip_download"}),
    path("<uuid:asset_id>/download.gif", views.legacy_redirect, {"to": "clip_download_gif"}),
    path("c/<uuid:asset_id>/", views.legacy_redirect, {"to": "clips_asset"}, name="clip_public"),
    path("c/<uuid:asset_id>.gif", views.legacy_redirect, {"to": "clip_public_gif"}),
    path("c/<uuid:asset_id>.mp4", views.legacy_redirect, {"to": "clip_public_mp4"}),
    path("clips/asset/<uuid:asset_id>/", views.legacy_redirect, {"to": "clips_asset"}),

    path("clips/", views.library, name="clips_library"),
    path("clips/record/", views.record_page, name="clips_record"),
    path("clips/upload/presign", views.presign, name="clips_presign"),
    path("clips/upload/finalize", views.finalize, name="clips_finalize"),
    path("clips/search/", RedirectView.as_view(pattern_name="clips_search", query_string=True, permanent=True)),
    path("clips/browse/", views.browse_page, name="clips_browse"),
    path("about/", views.about_page, name="clips_about"),
    path("clips/templates/", views.template_gallery, name="clips_templates"),
    path("clips/<uuid:asset_id>/remix/", views.remix_page, name="clips_remix"),
    path("clips/<uuid:asset_id>/remix", views.remix_create, name="clips_remix_create"),
    path("clips/asset/<uuid:asset_id>/status", views.asset_status, name="clips_asset_status"),
    path("clips/asset/<uuid:asset_id>/edit/", views.asset_edit, name="clips_edit"),
    path("clips/asset/<uuid:asset_id>/regenerate/", views.asset_regenerate, name="clips_regenerate"),
    path("clips/asset/<uuid:asset_id>/delete/", views.asset_delete, name="clips_delete"),
    path("clips/asset/<uuid:asset_id>/caption/", views.caption_builder, name="clips_caption"),
    path("clips/asset/<uuid:asset_id>/caption/save", views.caption_save, name="clips_caption_save"),
]

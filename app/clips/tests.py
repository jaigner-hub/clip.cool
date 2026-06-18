"""clips service-layer tests. Hermetic: the R2 (storage) and Typesense (search) seams are mocked,
so these run without boto3/typesense/Pillow or a live backend — they pin the *logic* that the
feature hinges on, not the I/O.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from django.urls import reverse

from clips import services
from clips.llm import LLMError
from clips.models import Asset, Template

User = get_user_model()


class FinalizeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u@example.com", "u@example.com")

    @patch("clips.tasks.process_asset")
    def test_finalize_creates_pending_and_enqueues_processing(self, mock_task):
        # WHY: the upload is direct-to-R2, so finalize is the only server-side record of the object.
        # It must persist a pending Asset owned by the uploader and hand processing to the worker —
        # never block the request on download/OCR.
        asset = services.finalize_asset(
            self.user, key="originals/abc/x.png", title="Hello", content_type="image/png", tags=["a"]
        )
        self.assertEqual(asset.status, Asset.Status.PENDING)
        self.assertEqual(asset.owner, self.user)
        self.assertEqual(asset.title, "Hello")
        mock_task.defer.assert_called_once_with(asset_id=str(asset.id))

    @patch("clips.tasks.process_asset")
    def test_finalize_leaves_title_blank_when_unnamed(self, mock_task):
        # WHY: we never expose/store the original filename — an unnamed upload has a blank title
        # (it's still findable by OCR text + tags), not the filename.
        asset = services.finalize_asset(self.user, key="originals/deadbeef.png", content_type="image/png")
        self.assertEqual(asset.title, "")

    @patch("clips.services.storage")
    def test_presigned_key_has_no_filename(self, mock_storage):
        # WHY: the object key is a random id + extension, never the user's filename.
        mock_storage.presign_put.return_value = "https://example.test/put"
        out = services.create_presigned_upload(self.user, "My Secret Meme.GIF", "image/gif")
        self.assertNotIn("secret", out["key"].lower())
        self.assertTrue(out["key"].startswith("originals/"))
        self.assertTrue(out["key"].endswith(".gif"))

    def test_presign_rejects_unsupported_type(self):
        # WHY: this is the image slice; only allow-listed image types get a signed PUT.
        with self.assertRaises(ValueError):
            services.create_presigned_upload(self.user, "x.exe", "application/x-msdownload")


class SearchScopingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u@example.com", "u@example.com")
        self.a1 = Asset.objects.create(owner=self.user, original_key="k1", status=Asset.Status.READY)
        self.a2 = Asset.objects.create(owner=self.user, original_key="k2", status=Asset.Status.READY)

    @patch("clips.services.search")
    def test_results_preserve_typesense_relevance_order(self, mock_search):
        # WHY: Typesense ranks the hits; the DB hydration must NOT reorder them (a naive
        # filter(pk__in=...) would return DB order and silently break relevance).
        mock_search.query.return_value = [str(self.a2.id), str(self.a1.id)]
        results = services.search_assets(self.user, "boyfriend")
        self.assertEqual([a.id for a in results], [self.a2.id, self.a1.id])

    @patch("clips.services.search")
    def test_non_superuser_is_scoped_to_own_assets(self, mock_search):
        # WHY: search is per-user; only a superuser queries across everyone (superuser-first).
        mock_search.query.return_value = []
        services.search_assets(self.user, "x")
        self.assertEqual(mock_search.query.call_args.kwargs["owner_id"], self.user.pk)

    @patch("clips.services.search")
    def test_superuser_sees_all(self, mock_search):
        self.user.is_superuser = True
        self.user.save()
        mock_search.query.return_value = []
        services.search_assets(self.user, "x")
        self.assertIsNone(mock_search.query.call_args.kwargs["owner_id"])


class AutodescribeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u@example.com", "u@example.com")

    def _asset(self, **kw):
        kw.setdefault("original_key", "originals/x.gif")
        kw.setdefault("poster_key", "posters/x.webp")
        kw.setdefault("status", Asset.Status.READY)
        return Asset.objects.create(owner=self.user, **kw)

    @patch("clips.llm.describe_image")
    @patch("clips.services.storage.download_bytes", return_value=b"imgbytes")
    @patch("clips.services.search")
    def test_merges_without_clobbering_human_title(self, mock_search, mock_dl, mock_describe):
        # WHY: vision metadata augments — it sets description, merges/dedupes tags, but a title the
        # user typed is authoritative and must survive.
        mock_describe.return_value = {
            "title": "AI Title", "description": "a dwarf in armor",
            "tags": ["dwarf", "reaction", "dwarf"],
        }
        a = self._asset(title="My Title", tags=["mine"])
        services.autodescribe_asset(str(a.id))
        a.refresh_from_db()
        self.assertEqual(a.title, "My Title")              # not clobbered
        self.assertEqual(a.description, "a dwarf in armor")
        self.assertEqual(a.tags, ["mine", "dwarf", "reaction"])  # merged + deduped
        mock_search.upsert.assert_called()

    @patch("clips.llm.describe_image")
    @patch("clips.services.storage.download_bytes", return_value=b"imgbytes")
    @patch("clips.services.search")
    def test_sets_title_when_blank(self, mock_search, mock_dl, mock_describe):
        mock_describe.return_value = {"title": "Auto Label", "description": "", "tags": []}
        a = self._asset(title="")
        services.autodescribe_asset(str(a.id))
        a.refresh_from_db()
        self.assertEqual(a.title, "Auto Label")

    @patch("clips.llm.describe_image", side_effect=LLMError("no key"))
    @patch("clips.services.storage.download_bytes", return_value=b"imgbytes")
    @patch("clips.services.search")
    def test_llm_failure_leaves_asset_untouched(self, mock_search, mock_dl, mock_describe):
        # WHY: auto-describe is best-effort; a missing key or API error must never break the asset.
        a = self._asset(title="", tags=["mine"])
        services.autodescribe_asset(str(a.id))  # must not raise
        a.refresh_from_db()
        self.assertEqual(a.title, "")
        self.assertEqual(a.tags, ["mine"])
        mock_search.upsert.assert_not_called()


class CaptionDedupTests(TestCase):
    def test_collapses_noisy_repeats_keeps_distinct(self):
        # WHY: a persistent GIF caption OCRs slightly differently per frame — collapse those to one
        # (the longest/cleanest), but keep genuinely different captions.
        out = services._dedup_captions([
            "TELL ME, WHERE IS FUCKING. GANDALF?",
            "TELL ME, WHERE Li) FUCKING GANDALF?",
            "TELL ME, WHERE IS FUCKING GANDALF?",   # cleanest/longest-ish of the cluster
            "ONE DOES NOT SIMPLY WALK INTO MORDOR",  # distinct caption
        ])
        self.assertEqual(len(out), 2)
        self.assertIn("ONE DOES NOT SIMPLY WALK INTO MORDOR", out)
        self.assertTrue(any("GANDALF" in o for o in out))

    def test_empty(self):
        self.assertEqual(services._dedup_captions([]), [])


class EditAndAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u@example.com", "u@example.com")
        self.other = User.objects.create_user("o@example.com", "o@example.com")
        self.asset = Asset.objects.create(
            owner=self.user, original_key="originals/x.gif",
            status=Asset.Status.READY, title="old", tags=["a"],
        )

    @patch("clips.services.search")
    def test_update_saves_and_reindexes(self, mock_search):
        a = services.update_asset(
            self.user, str(self.asset.id), title="New", description="d", tags=["x", "x", "Y"]
        )
        self.assertIsNotNone(a)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.title, "New")
        self.assertEqual(self.asset.description, "d")
        self.assertEqual(self.asset.tags, ["x", "Y"])   # deduped (case-insensitive), order kept
        mock_search.upsert.assert_called_once()

    def test_other_user_cannot_see_or_edit(self):
        # WHY: clips are per-user; another user must not read or mutate them.
        self.assertIsNone(services.get_asset_for(self.other, str(self.asset.id)))
        self.assertIsNone(services.update_asset(self.other, str(self.asset.id), title="hacked"))
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.title, "old")       # unchanged

    def test_superuser_can_see_any(self):
        self.other.is_superuser = True
        self.other.save()
        self.assertIsNotNone(services.get_asset_for(self.other, str(self.asset.id)))

    @patch("clips.tasks.autodescribe_asset")
    def test_regenerate_enqueues_with_force_title(self, mock_task):
        a = services.regenerate_asset(self.user, str(self.asset.id))
        self.assertIsNotNone(a)
        mock_task.defer.assert_called_once_with(asset_id=str(self.asset.id), force_title=True)


class TemplateBuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u@example.com", "u@example.com")
        self.client.force_login(self.user)
        self.t = Template.objects.create(
            name="Drake Hotline Bling", image_key="templates/imgflip/181913649.jpg",
            mime="image/jpeg", width=1200, height=1200, source="imgflip", source_id="181913649",
        )

    def test_helpers(self):
        self.assertEqual(services.get_template(str(self.t.id)).name, "Drake Hotline Bling")
        self.assertEqual(len(services.list_templates()), 1)

    def test_gallery_lists_templates(self):
        r = self.client.get(reverse("clips_create"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Drake Hotline Bling")

    def test_builder_page_renders(self):
        r = self.client.get(reverse("clips_builder", args=[self.t.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Add text")
        self.assertContains(r, "meme-canvas")

    @patch("clips.services.template_image_bytes", return_value=b"\x89PNGfake")
    def test_template_image_proxy_same_origin(self, mock_bytes):
        # WHY: served same-origin so the builder canvas can export without tainting.
        r = self.client.get(reverse("clips_template_image", args=[self.t.id]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        self.assertEqual(r.content, b"\x89PNGfake")


class VisibilityTests(TestCase):
    def test_superuser_filter_is_none(self):
        from clips import search
        self.assertIsNone(search._filter_for(None))

    def test_user_filter_is_public_or_own(self):
        # WHY: a regular viewer sees the shared public catalog plus their own (incl. private) clips.
        from clips import search
        self.assertEqual(search._filter_for(7), "is_public:=true || owner_id:=7")

    @patch("clips.services.search")
    def test_update_can_make_private(self, mock_search):
        user = User.objects.create_user("v@example.com", "v@example.com")
        a = Asset.objects.create(owner=user, original_key="k", status=Asset.Status.READY)
        self.assertTrue(a.is_public)  # default public
        services.update_asset(user, str(a.id), is_public=False)
        a.refresh_from_db()
        self.assertFalse(a.is_public)
        mock_search.upsert.assert_called_once()


class VideoIngestTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("vid@example.com", "vid@example.com")

    def test_media_type_detection(self):
        self.assertEqual(services._media_type("video/mp4"), Asset.MediaType.VIDEO)
        self.assertEqual(services._media_type("image/gif"), Asset.MediaType.VIDEO)   # GIF → video
        self.assertEqual(services._media_type("image/png"), Asset.MediaType.IMAGE)

    @patch("clips.tasks.transcode_asset")
    @patch("clips.tasks.process_asset")
    def test_video_finalize_routes_to_transcode(self, mock_process, mock_transcode):
        # WHY: video/GIF go to the ffmpeg transcode tier, not the image poster/OCR path.
        a = services.finalize_asset(self.user, key="originals/x.gif", content_type="image/gif")
        self.assertEqual(a.media_type, Asset.MediaType.VIDEO)
        mock_transcode.defer.assert_called_once_with(asset_id=str(a.id))
        mock_process.defer.assert_not_called()

    @patch("clips.tasks.transcode_asset")
    @patch("clips.tasks.process_asset")
    def test_image_finalize_routes_to_process(self, mock_process, mock_transcode):
        a = services.finalize_asset(self.user, key="originals/x.png", content_type="image/png")
        self.assertEqual(a.media_type, Asset.MediaType.IMAGE)
        mock_process.defer.assert_called_once_with(asset_id=str(a.id))
        mock_transcode.defer.assert_not_called()

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + k)
    def test_video_sources_ordered_av1_vp9_h264(self, mock_url):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="o.mp4",
                                 media_type=Asset.MediaType.VIDEO, status=Asset.Status.READY)
        Rendition.objects.create(asset=a, kind=Rendition.Kind.H264, r2_key="r/h", mime="video/mp4")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.AV1, r2_key="r/a", mime="video/mp4")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.VP9, r2_key="r/v", mime="video/webm")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.POSTER, r2_key="r/p", mime="image/webp")
        kinds = [s["kind"] for s in services.video_sources(a)]
        self.assertEqual(kinds, ["av1", "vp9", "h264"])   # ordered, poster excluded


class CaptionOverlayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("cap@example.com", "cap@example.com")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(
            owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
            width=640, height=360, status=Asset.Status.READY,
        )

    @patch("clips.services.search")
    def test_save_caption_stores_layers_and_indexes_typed_text(self, mock_search):
        # WHY: we know the exact typed caption — store the editable layers + PNG key, and index the
        # text directly (no OCR of the rendered overlay).
        layers = [
            {"text": "top", "cx": 0.5, "cy": 0.1, "w": 0.8, "size": 0.1},
            {"text": "bottom", "cx": 0.5, "cy": 0.9, "w": 0.8, "size": 0.1},
        ]
        a = services.save_caption(self.user, str(self.asset.id), text_key="captions/x.png", layers=layers)
        self.assertIsNotNone(a)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.text_layer_key, "captions/x.png")
        self.assertEqual(self.asset.caption_layers, layers)
        self.assertEqual(self.asset.ocr_text, "top bottom")
        mock_search.upsert.assert_called_once()

    def test_other_user_cannot_caption(self):
        other = User.objects.create_user("o2@example.com", "o2@example.com")
        self.assertIsNone(services.save_caption(other, str(self.asset.id), text_key="x", layers=[]))

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_caption_page_renders_overlay_mode(self, mock_url):
        self.asset.caption_layers = [{"text": "hi", "cx": 0.5, "cy": 0.5, "w": 0.8, "size": 0.1}]
        self.asset.save()
        r = self.client.get(reverse("clips_caption", args=[self.asset.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'data-mode="overlay"')
        self.assertContains(r, "meme-canvas")


class PublicSharePageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("share@example.com", "share@example.com")

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_canonical_page_visible_without_login_with_og(self, mock_url):
        # WHY: the canonical clip.cool/<id> page must work for logged-out viewers AND carry OG meta
        # so chat/social unfurls (it replaces the old /c/<id> share page).
        a = Asset.objects.create(owner=self.user, original_key="o.png", media_type=Asset.MediaType.IMAGE,
                                 status=Asset.Status.READY, is_public=True, title="Shared")
        r = self.client.get(reverse("clips_asset", args=[a.id]))   # no login
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'property="og:title"')   # OG meta for unfurls
        self.assertContains(r, "Shared")

    def test_old_c_path_301s_to_canonical(self):
        # WHY: links already shared as /c/<id> must keep working (permanent redirect to /<id>).
        a = Asset.objects.create(owner=self.user, original_key="o.png", media_type=Asset.MediaType.IMAGE,
                                 status=Asset.Status.READY, is_public=True)
        r = self.client.get(reverse("clip_public", args=[a.id]))
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], reverse("clips_asset", args=[a.id]))

    def test_private_clip_is_404(self):
        a = Asset.objects.create(owner=self.user, original_key="o.png", status=Asset.Status.READY, is_public=False)
        self.assertEqual(self.client.get(reverse("clips_asset", args=[a.id])).status_code, 404)

    def test_unready_clip_is_404(self):
        a = Asset.objects.create(owner=self.user, original_key="o.png", status=Asset.Status.TRANSCODING, is_public=True)
        self.assertEqual(self.client.get(reverse("clips_asset", args=[a.id])).status_code, 404)


class PublicMp4LinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mp4@example.com", "mp4@example.com")

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_mp4_link_redirects_to_h264(self, mock_url):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=True)
        Rendition.objects.create(asset=a, kind=Rendition.Kind.H264, r2_key="r/h264.mp4", mime="video/mp4")
        r = self.client.get(reverse("clip_public_mp4", args=[a.id]))   # no login
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "https://cdn/r/h264.mp4")

    def test_mp4_link_private_404(self):
        a = Asset.objects.create(owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=False)
        self.assertEqual(self.client.get(reverse("clip_public_mp4", args=[a.id])).status_code, 404)


class PublicGifLinkTests(TestCase):
    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_gif_link_redirects_to_gif_rendition(self, mock_url):
        from clips.models import Rendition
        u = User.objects.create_user("g@example.com", "g@example.com")
        a = Asset.objects.create(owner=u, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=True)
        Rendition.objects.create(asset=a, kind=Rendition.Kind.GIF, r2_key="r/preview.gif", mime="image/gif")
        r = self.client.get(reverse("clip_public_gif", args=[a.id]))   # no login
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "https://cdn/r/preview.gif")

    def test_gif_link_404_without_rendition(self):
        u = User.objects.create_user("g2@example.com", "g2@example.com")
        a = Asset.objects.create(owner=u, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=True)
        self.assertEqual(self.client.get(reverse("clip_public_gif", args=[a.id])).status_code, 404)


class PublicBrowseTests(TestCase):
    def _img(self, owner, **kw):
        kw.setdefault("media_type", Asset.MediaType.IMAGE)
        kw.setdefault("status", Asset.Status.READY)
        return Asset.objects.create(owner=owner, original_key="o.png", **kw)

    def test_filter_public_only(self):
        from clips import search
        self.assertEqual(search._filter_for(None, public_only=True), "is_public:=true")

    @patch("clips.services.search")
    def test_anonymous_search_is_public_only(self, mock_search):
        from django.contrib.auth.models import AnonymousUser
        mock_search.query.return_value = []
        services.search_assets(AnonymousUser(), "x")
        self.assertTrue(mock_search.query.call_args.kwargs.get("public_only"))

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_public_detail_visible_logged_out_without_edit_controls(self, mock_url):
        u = User.objects.create_user("p@example.com", "p@example.com")
        a = self._img(u, is_public=True, title="Pub")
        r = self.client.get(reverse("clips_asset", args=[a.id]))   # no login
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pub")
        self.assertNotContains(r, "Regenerate AI labels")          # owner-only, hidden

    def test_private_detail_404_logged_out(self):
        u = User.objects.create_user("p2@example.com", "p2@example.com")
        a = self._img(u, is_public=False)
        self.assertEqual(self.client.get(reverse("clips_asset", args=[a.id])).status_code, 404)

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_owner_sees_edit_controls(self, mock_url):
        u = User.objects.create_user("p3@example.com", "p3@example.com")
        a = self._img(u, is_public=True)
        self.client.force_login(u)
        r = self.client.get(reverse("clips_asset", args=[a.id]))
        self.assertContains(r, "Regenerate AI labels")            # owner sees controls


class BrowseTests(TestCase):
    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_browse_grid_logged_out_public_only(self, mock_url):
        u = User.objects.create_user("b@example.com", "b@example.com")
        Asset.objects.create(owner=u, original_key="o.png", media_type=Asset.MediaType.IMAGE,
                             status=Asset.Status.READY, is_public=True, title="PubBrowse")
        Asset.objects.create(owner=u, original_key="p.png", media_type=Asset.MediaType.IMAGE,
                             status=Asset.Status.READY, is_public=False, title="PrivBrowse")
        r = self.client.get(reverse("clips_browse"))   # no login
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PubBrowse")
        self.assertNotContains(r, "PrivBrowse")        # private never surfaced

    def test_browse_assets_excludes_private_and_unready(self):
        u = User.objects.create_user("b2@example.com", "b2@example.com")
        Asset.objects.create(owner=u, original_key="o.png", status=Asset.Status.READY, is_public=True)
        Asset.objects.create(owner=u, original_key="o2.png", status=Asset.Status.TRANSCODING, is_public=True)
        Asset.objects.create(owner=u, original_key="o3.png", status=Asset.Status.READY, is_public=False)
        self.assertEqual(len(services.browse_assets()), 1)


class RootSearchTests(TestCase):
    def test_root_serves_search_directly(self):
        # WHY: "/" IS the search surface now (no redirect hop).
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "clip-searchbar")

    def test_old_search_path_301s_to_root_preserving_query(self):
        r = self.client.get("/clips/search/?q=gollum")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/?q=gollum")


class DownloadLinkTests(TestCase):
    @patch("clips.services.storage.presign_get", return_value="https://r2/dl?response-content-disposition=attachment")
    def test_download_redirects_to_attachment_presigned_url(self, mock_presign):
        u = User.objects.create_user("d@example.com", "d@example.com")
        a = Asset.objects.create(owner=u, original_key="originals/x.gif", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=True, title="My Clip")
        r = self.client.get(reverse("clip_download", args=[a.id]))   # no login (public)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "https://r2/dl?response-content-disposition=attachment")
        # WHY: forced filename is the clip title + original extension (sanitized), for a sensible save.
        self.assertEqual(mock_presign.call_args.kwargs["filename"], "My Clip.gif")

    def test_download_private_404_logged_out(self):
        u = User.objects.create_user("d2@example.com", "d2@example.com")
        a = Asset.objects.create(owner=u, original_key="originals/x.gif", status=Asset.Status.READY, is_public=False)
        self.assertEqual(self.client.get(reverse("clip_download", args=[a.id])).status_code, 404)


class DeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("del@example.com", "del@example.com")
        self.other = User.objects.create_user("other@example.com", "other@example.com")

    @patch("clips.services.search")
    @patch("clips.services.storage")
    def test_delete_removes_r2_index_and_rows(self, mock_storage, mock_search):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="originals/x.gif", poster_key="posters/x.webp",
                                 media_type=Asset.MediaType.VIDEO, status=Asset.Status.READY)
        Rendition.objects.create(asset=a, kind=Rendition.Kind.H264, r2_key="renditions/x/h264.mp4", mime="video/mp4")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.GIF, r2_key="renditions/x/preview.gif", mime="image/gif")
        self.assertTrue(services.delete_asset(self.user, a.id))
        # every R2 object deleted (original, poster, both renditions); index doc removed; rows gone.
        deleted = {c.args[0] for c in mock_storage.delete.call_args_list}
        self.assertEqual(deleted, {"originals/x.gif", "posters/x.webp", "renditions/x/h264.mp4", "renditions/x/preview.gif"})
        mock_search.remove.assert_called_once_with(a.id)
        self.assertFalse(Asset.objects.filter(pk=a.id).exists())
        self.assertEqual(Rendition.objects.filter(asset_id=a.id).count(), 0)

    @patch("clips.services.search")
    @patch("clips.services.storage")
    def test_cannot_delete_someone_elses_clip(self, mock_storage, mock_search):
        a = Asset.objects.create(owner=self.user, original_key="o.gif", status=Asset.Status.READY)
        self.assertIsNone(services.delete_asset(self.other, a.id))   # not owner → no-op
        self.assertTrue(Asset.objects.filter(pk=a.id).exists())
        mock_storage.delete.assert_not_called()

    def test_delete_view_requires_post_and_owner(self):
        a = Asset.objects.create(owner=self.user, original_key="o.gif", status=Asset.Status.READY)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("clips_delete", args=[a.id])).status_code, 404)  # not owner
        self.assertTrue(Asset.objects.filter(pk=a.id).exists())

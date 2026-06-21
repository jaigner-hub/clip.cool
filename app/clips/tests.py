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
from clips.models import Asset, Rendition, Template

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


class ListAssetsScopingTests(TestCase):
    def setUp(self):
        self.me = User.objects.create_user("me@example.com", "me@example.com")
        self.other = User.objects.create_user("other@example.com", "other@example.com")
        self.mine = Asset.objects.create(owner=self.me, original_key="mine", status=Asset.Status.READY)
        self.theirs = Asset.objects.create(owner=self.other, original_key="theirs", status=Asset.Status.READY)

    def test_owner_scoped(self):
        # WHY: "My clips" means MINE — only the caller's own assets, never another user's.
        self.assertEqual([a.id for a in services.list_assets(self.me)], [self.mine.id])

    def test_superuser_still_only_sees_own(self):
        # WHY: unlike search, "My clips" / the API's "List your assets" stay owner-scoped even for a
        # superuser — otherwise an admin opening their own library leaks everyone's clips. A superuser
        # still reaches any single clip via get_asset_for and sees all via Browse / the Django admin.
        self.me.is_superuser = True
        self.me.save()
        self.assertEqual([a.id for a in services.list_assets(self.me)], [self.mine.id])


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
        # The Create/meme-builder UI was removed, but the Template model + service helpers are
        # retained so seeding can be revived later. Guard the helpers stay working.
        self.assertEqual(services.get_template(str(self.t.id)).name, "Drake Hotline Bling")
        self.assertEqual(len(services.list_templates()), 1)


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

    @patch("clips.tasks.burn_caption_asset")
    @patch("clips.services.search")
    def test_save_caption_stores_layers_and_indexes_typed_text(self, mock_search, mock_burn):
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


class SeoTests(TestCase):
    """The crawl/index surfaces Google needs: canonical meta, robots.txt, sitemap.xml."""

    def setUp(self):
        self.user = User.objects.create_user("seo@example.com", "seo@example.com")

    def test_landing_pages_carry_canonical_and_description(self):
        # WHY: every indexable page needs a canonical URL pinned to the one public origin (so the
        # dual-served app.vent.dog host doesn't split ranking) and a meta description.
        r = self.client.get(reverse("clips_about"))
        self.assertContains(r, 'rel="canonical" href="https://clip.cool/about/"')
        self.assertContains(r, 'name="description"')
        self.assertContains(r, 'property="og:title"')

    def test_robots_allows_crawl_and_points_at_sitemap(self):
        # WHY: a missing/blocking robots.txt is the classic "site won't index" foot-gun.
        r = self.client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["content-type"], "text/plain")
        body = r.content.decode()
        self.assertIn("Sitemap: https://clip.cool/sitemap.xml", body)
        self.assertIn("Disallow: /admin/", body)

    def test_sitemap_lists_static_pages_and_public_clips_only(self):
        # WHY: the sitemap must include public clips for discovery but never leak private ones.
        pub = Asset.objects.create(owner=self.user, original_key="p", status=Asset.Status.READY, is_public=True)
        priv = Asset.objects.create(owner=self.user, original_key="x", status=Asset.Status.READY, is_public=False)
        r = self.client.get("/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["content-type"], "application/xml")
        body = r.content.decode()
        self.assertIn("https://clip.cool/about/", body)
        self.assertIn(f"https://clip.cool/{pub.id}", body)
        self.assertNotIn(str(priv.id), body)


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

    @patch("clips.services.storage.presign_get", return_value="https://r2/dl")
    def test_video_download_serves_the_mp4_not_the_webm_original(self, mock_presign):
        # WHY: the "Download MP4" button must hand back an actual .mp4 (the H.264 rendition), never the
        # raw .webm original — that's what made the prominent Download give a surprising file. The
        # "Original file" link is the only path to the source.
        from clips.models import Rendition
        u = User.objects.create_user("mp4@example.com", "mp4@example.com")
        a = Asset.objects.create(owner=u, original_key="originals/x.webm", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, is_public=True, title="Clip")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.H264, r2_key="renditions/x/h264.mp4", mime="video/mp4")
        self.client.get(reverse("clip_download", args=[a.id]))
        self.assertEqual(mock_presign.call_args.args[0], "renditions/x/h264.mp4")
        self.assertEqual(mock_presign.call_args.kwargs["filename"], "Clip.mp4")


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


class CaptionBurnTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("burn@example.com", "burn@example.com")

    @patch("clips.services.search")
    @patch("clips.tasks.burn_caption_asset")
    def test_save_caption_enqueues_burn(self, mock_burn, mock_search):
        a = Asset.objects.create(owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY)
        services.save_caption(self.user, a.id, text_key="texts/x.png", layers=[{"text": "TOP"}])
        mock_burn.defer.assert_called_once_with(asset_id=str(a.id))

    @patch("clips.services.search")
    @patch("clips.services.storage")
    @patch("clips.tasks.burn_caption_asset")
    def test_clearing_caption_drops_burned_rendition(self, mock_burn, mock_storage, mock_search):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, text_layer_key="texts/x.png")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.CAPTIONED, r2_key="r/captioned.mp4", mime="video/mp4")
        services.save_caption(self.user, a.id, text_key="", layers=[])   # caption removed
        mock_storage.delete.assert_called_once_with("r/captioned.mp4")
        self.assertFalse(Rendition.objects.filter(asset=a, kind=Rendition.Kind.CAPTIONED).exists())
        mock_burn.defer.assert_not_called()

    @patch("clips.services.storage.presign_get", side_effect=lambda k, **kw: "https://r2/" + k)
    def test_download_prefers_captioned(self, mock_presign):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="originals/x.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, title="Cap")
        # no captioned yet → original
        services.download_url(a)
        self.assertEqual(mock_presign.call_args.args[0], "originals/x.mp4")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.CAPTIONED, r2_key="renditions/x/captioned.mp4", mime="video/mp4")
        services.download_url(a)
        self.assertEqual(mock_presign.call_args.args[0], "renditions/x/captioned.mp4")
        self.assertEqual(mock_presign.call_args.kwargs["filename"], "Cap.mp4")

    @patch("clips.transcode.burn_caption", return_value="/tmp/captioned.mp4")
    @patch("clips.services.storage")
    def test_burn_caption_asset_makes_rendition(self, mock_storage, mock_burn):
        from clips.models import Rendition
        a = Asset.objects.create(owner=self.user, original_key="originals/x.mp4", media_type=Asset.MediaType.VIDEO,
                                 status=Asset.Status.READY, text_layer_key="texts/x.png")
        Rendition.objects.create(asset=a, kind=Rendition.Kind.H264, r2_key="renditions/x/h264.mp4", mime="video/mp4")
        mock_storage.download_bytes.return_value = b"x"
        from unittest.mock import mock_open
        with patch("builtins.open", mock_open(read_data=b"captioned")):
            services.burn_caption_asset(str(a.id))
        # burned onto the H.264 rendition, uploaded as the captioned rendition
        up = mock_storage.upload_bytes.call_args
        self.assertTrue(up.args[0].endswith("captioned.mp4"))
        self.assertTrue(Rendition.objects.filter(asset=a, kind=Rendition.Kind.CAPTIONED).exists())


class TemplateLibraryTests(TestCase):
    """The public template library: recorded clips anyone can browse + remix."""

    def setUp(self):
        self.user = User.objects.create_user("rec@example.com", "rec@example.com")

    @patch("clips.tasks.transcode_asset")
    def test_finalize_flags_recorded_video(self, mock_transcode):
        # WHY: the recorder posts from_recorder=True so the clip joins the library once public+ready.
        a = services.finalize_asset(
            self.user, key="originals/r.webm", content_type="video/webm", from_recorder=True
        )
        self.assertTrue(a.from_recorder)

    @patch("clips.tasks.process_asset")
    def test_finalize_recorder_flag_ignored_for_image(self, mock_process):
        # WHY: only a recorded *video* is a template — an image can't be "recorded".
        a = services.finalize_asset(
            self.user, key="originals/x.png", content_type="image/png", from_recorder=True
        )
        self.assertFalse(a.from_recorder)

    @patch("clips.tasks.transcode_asset")
    def test_upload_is_not_a_template(self, mock_transcode):
        # WHY: a plain video upload (no flag) must never enter the template library.
        a = services.finalize_asset(self.user, key="originals/x.webm", content_type="video/webm")
        self.assertFalse(a.from_recorder)

    @patch("clips.tasks.transcode_asset")
    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_finalize_view_forwards_recorder_flag(self, mock_public_url, mock_transcode):
        # WHY: the recorder POSTs from_recorder=True to the finalize *view*. A regression dropped it
        # in the view (the service still honored it), so every recording silently entered as a plain
        # upload and the template library stayed empty. Guard the full request path, not the service.
        self.client.force_login(self.user)
        res = self.client.post(
            reverse("clips_finalize"),
            data={"key": "originals/r.webm", "content_type": "video/webm", "from_recorder": True},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Asset.objects.get(id=res.json()["id"]).from_recorder)

    def _rec(self, **kw):
        defaults = dict(
            owner=self.user, original_key="o.mp4", media_type=Asset.MediaType.VIDEO,
            status=Asset.Status.READY, is_public=True, from_recorder=True,
        )
        defaults.update(kw)
        return Asset.objects.create(**defaults)

    def test_list_template_clips_membership(self):
        keep = self._rec(title="in")
        self._rec(is_public=False, title="private")        # opted out via private
        self._rec(from_recorder=False, title="uploaded")   # not a recording
        self._rec(status=Asset.Status.TRANSCODING, title="not-ready")
        self._rec(media_type=Asset.MediaType.IMAGE, title="image")
        ids = [a.id for a in services.list_template_clips()]
        self.assertEqual(ids, [keep.id])   # only the public, ready, recorded video

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_gallery_public_no_login(self, mock_url):
        # WHY: "anyone can access" — the library renders for a logged-out visitor.
        self._rec(title="Funny reaction")
        r = self.client.get(reverse("clips_templates"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Funny reaction")

    def test_remix_page_requires_login(self):
        a = self._rec()
        r = self.client.get(reverse("clips_remix", args=[a.id]))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/oidc/", r["Location"] + "")  # bounced to auth


class RemixTests(TestCase):
    """create_remix clones a template into a NEW owned clip; the source is never mutated."""

    def setUp(self):
        self.owner = User.objects.create_user("src@example.com", "src@example.com")
        self.remixer = User.objects.create_user("mix@example.com", "mix@example.com")
        self.source = Asset.objects.create(
            owner=self.owner, original_key="originals/src.webm", media_type=Asset.MediaType.VIDEO,
            status=Asset.Status.READY, is_public=True, from_recorder=True, title="Template",
        )
        Rendition.objects.create(
            asset=self.source, kind=Rendition.Kind.H264, r2_key="renditions/src/h264.mp4",
            mime="video/mp4",
        )

    @patch("clips.tasks.transcode_asset")
    @patch("clips.services.storage.copy")
    def test_remix_clones_into_new_owned_asset(self, mock_copy, mock_transcode):
        a = services.create_remix(
            self.remixer, str(self.source.id),
            crop={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, trim_start=1.0, trim_end=3.0,
            title="My remix", tags=["mine"],
        )
        self.assertIsNotNone(a)
        # New, independent clip owned by the remixer, with lineage + the remixer's crop/trim.
        self.assertEqual(a.owner, self.remixer)
        self.assertEqual(a.remixed_from_id, self.source.id)
        self.assertEqual(a.status, Asset.Status.PENDING)
        self.assertEqual(a.media_type, Asset.MediaType.VIDEO)
        self.assertFalse(a.from_recorder)              # a remix isn't itself a recording
        self.assertEqual(a.crop, {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5})
        self.assertEqual((a.trim_start, a.trim_end), (1.0, 3.0))
        # Copied FROM the H.264 rendition (what the user saw) INTO the new original key.
        src_key, dst_key = mock_copy.call_args.args
        self.assertEqual(src_key, "renditions/src/h264.mp4")
        self.assertEqual(dst_key, a.original_key)
        self.assertTrue(a.original_key.startswith("originals/"))
        mock_transcode.defer.assert_called_once_with(asset_id=str(a.id))
        # Source untouched.
        self.source.refresh_from_db()
        self.assertEqual(self.source.owner, self.owner)
        self.assertEqual(self.source.original_key, "originals/src.webm")

    @patch("clips.tasks.transcode_asset")
    @patch("clips.services.storage.copy")
    def test_remix_falls_back_to_original_without_h264(self, mock_copy, mock_transcode):
        # WHY: if the source has no H.264 rendition yet, clone the original rather than failing.
        self.source.renditions.all().delete()
        a = services.create_remix(self.remixer, str(self.source.id))
        self.assertIsNotNone(a)
        self.assertEqual(mock_copy.call_args.args[0], "originals/src.webm")

    @patch("clips.services.storage.public_url", side_effect=lambda k: "https://cdn/" + (k or ""))
    def test_remix_page_renders_for_user(self, mock_url):
        # WHY: smoke the editor template + that the H.264 source URL is wired in for remix.js.
        self.client.force_login(self.remixer)
        r = self.client.get(reverse("clips_remix", args=[self.source.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Create my GIF")
        self.assertContains(r, "renditions/src/h264.mp4")   # data-src-url

    @patch("clips.services.storage.copy")
    def test_remix_rejects_private_image_or_missing(self, mock_copy):
        # WHY: only a public, ready VIDEO is a remixable template.
        import uuid as _uuid
        self.source.is_public = False
        self.source.save()
        self.assertIsNone(services.create_remix(self.remixer, str(self.source.id)))
        img = Asset.objects.create(
            owner=self.owner, original_key="i.png", media_type=Asset.MediaType.IMAGE,
            status=Asset.Status.READY, is_public=True,
        )
        self.assertIsNone(services.create_remix(self.remixer, str(img.id)))
        self.assertIsNone(services.create_remix(self.remixer, str(_uuid.uuid4())))
        mock_copy.assert_not_called()   # never touch storage for an ineligible source

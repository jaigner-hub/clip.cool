"""clips service-layer tests. Hermetic: the R2 (storage) and Typesense (search) seams are mocked,
so these run without boto3/typesense/Pillow or a live backend — they pin the *logic* that the
feature hinges on, not the I/O.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from clips import services
from clips.models import Asset

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
        asset = services.finalize_asset(self.user, key="originals/deadbeef.gif", content_type="image/gif")
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

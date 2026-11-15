import io

from app.extensions import db
from app.models import FeedPost, FeedPostLike, FeedReport, Notification
from tests.conftest import png_bytes

J = {"X-Requested-With": "fetch"}


def _post(c, b, text="Hello group", **kw):
    r = c.post(b + "/community/posts", data={"text": text, "content": text, "post_text": text, "category": "General Updates", **kw},
               headers=J)
    return r


def test_post_like_comment_reply_share_report_flow(app, world):
    b = f"/g/{world.ga.id}"
    r = _post(world.cm, b)
    assert r.status_code in (200, 201), r.data
    p = FeedPost.query.filter_by(group_id=world.ga.id).first()
    assert p is not None
    # likes are unique
    world.ca.post(f"{b}/community/posts/{p.id}/like", headers=J)
    world.ca.post(f"{b}/community/posts/{p.id}/like", headers=J)
    assert FeedPostLike.query.filter_by(post_id=p.id).count() == 1
    assert Notification.query.filter_by(user_id=world.mem_a.id).count() >= 1
    world.ca.post(f"{b}/community/posts/{p.id}/unlike", headers=J)
    assert FeedPostLike.query.filter_by(post_id=p.id).count() == 0
    # comments and replies
    rc = world.ca.post(f"{b}/community/posts/{p.id}/comments", data={"text": "Nice"}, headers=J)
    assert rc.status_code == 200
    from app.models import FeedPostComment
    c = FeedPostComment.query.first()
    rr = world.cm.post(f"{b}/community/comments/{c.id}/replies", data={"text": "Thanks"}, headers=J)
    assert rr.status_code == 200
    assert world.ca.get(f"{b}/community/posts/{p.id}/comments", headers=J).status_code == 200
    # share (same group) and report
    assert world.ca.post(f"{b}/community/posts/{p.id}/share", data={"description": "see this"}, headers=J).status_code == 200
    assert world.ca.post(f"{b}/community/posts/{p.id}/report", data={"reason": "Spam"}, headers=J).status_code == 200
    world.ca.post(f"{b}/community/posts/{p.id}/report", data={"reason": "Spam"}, headers=J)
    assert FeedReport.query.count() == 1  # duplicate report blocked
    assert world.ca.get(b + "/community/reports").status_code == 200


def test_member_cannot_edit_others_post_and_admin_can_delete(app, world):
    b = f"/g/{world.ga.id}"
    _post(world.ca, b, "Admin post")
    p = FeedPost.query.first()
    assert world.cm.post(f"{b}/community/posts/{p.id}/edit", data={"text": "hack"}, headers=J).status_code in (403, 404)
    assert world.cm.post(f"{b}/community/posts/{p.id}/delete", data={"reason": "x"}, headers=J).status_code in (403, 404)
    assert world.ca.post(f"{b}/community/posts/{p.id}/pin", data={"pin": "1"}, headers=J).status_code == 200
    assert world.ca.post(f"{b}/community/posts/{p.id}/delete", data={"reason": "cleanup"}, headers=J).status_code == 200
    db.session.expire_all()
    assert db.session.get(FeedPost, p.id).deleted_at is not None  # soft delete


def test_community_isolation(app, world):
    b, bb = f"/g/{world.ga.id}", f"/g/{world.gb.id}"
    _post(world.ca, b, "Private to A")
    p = FeedPost.query.first()
    assert world.cb.get(f"{bb}/community/posts/{p.id}").status_code == 404
    assert world.cb.post(f"{bb}/community/posts/{p.id}/like", headers=J).status_code == 404
    assert world.cb.post(f"{bb}/community/posts/{p.id}/comments", data={"text": "x"}, headers=J).status_code == 404
    assert world.cb.post(f"{bb}/community/posts/{p.id}/share", headers=J).status_code == 404
    assert b"Private to A" not in world.cb.get(bb + "/community/posts").data
    assert b"Private to A" not in world.cb.get(bb + "/feed/").data


def test_image_attachment_goes_to_uploadcare(app, world):
    b = f"/g/{world.ga.id}"
    r = world.cm.post(b + "/community/posts", data={"text": "pic", "content": "pic", "category": "Celebrations",
                                                    "files": (io.BytesIO(png_bytes()), "p.png")},
                      content_type="multipart/form-data", headers=J)
    assert r.status_code in (200, 201), r.data
    from app.models import FeedAttachment, StoredFile
    att = FeedAttachment.query.first()
    assert att is not None
    assert db.session.get(StoredFile, att.file_id).provider == "uploadcare"
    assert world.cb.get(f"/files/{att.file_id}").status_code in (403, 404)

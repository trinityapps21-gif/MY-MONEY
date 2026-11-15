"""Announcements, automated Financial Activity Feed and the Community Social Feed."""
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.models.core import utcnow

FEED_EVENT_TYPES = (
    "CONTRIBUTION", "LOAN_APPROVED", "LOAN_DISBURSED", "REPAYMENT",
    "LOAN_REPAID", "MEMBER_APPROVED", "ANNOUNCEMENT", "MILESTONE",
)
POST_CATEGORIES = (
    "General Updates", "Group Activities", "Meetings", "Celebrations",
    "Important Information", "Member Updates", "Other",
)
REPORT_REASONS = ("Spam", "Inappropriate content", "Harassment", "Misleading information", "Other")


# --------------------------------------------------------------- Announcements
class Announcement(db.Model):
    __tablename__ = "announcements"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    content = db.Column(db.Text, nullable=False)
    publish_date = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_published = db.Column(db.Boolean, nullable=False, default=True)
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(10), nullable=False, default="ACTIVE")
    attachment_file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    removed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    removed_at = db.Column(db.DateTime)

    author = db.relationship("User", foreign_keys=[created_by])
    attachment = db.relationship("StoredFile", foreign_keys=[attachment_file_id])

    __table_args__ = (CheckConstraint("status IN ('ACTIVE','REMOVED')", name="ck_announce_status"),)


class AnnouncementHistory(db.Model):
    __tablename__ = "announcement_history"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey("announcements.id"), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)
    snapshot = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# ------------------------------------------------- Financial Activity Feed (auto)
class GroupFeed(db.Model):
    """Automatically generated financial activity timeline. Members cannot
    create or alter these rows. Descriptions never contain borrower details."""
    __tablename__ = "group_feed"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.String(300))
    amount = db.Column(db.BigInteger)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    ref_type = db.Column(db.String(30))
    ref_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    actor = db.relationship("User")

    __table_args__ = (db.Index("ix_feed_group_created", "group_id", "created_at"),)


# ------------------------------------------------------- Community Social Feed
class FeedPost(db.Model):
    __tablename__ = "feed_posts"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    author_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    category = db.Column(db.String(40), nullable=False, default="General Updates")
    status = db.Column(db.String(12), nullable=False, default="PUBLISHED")
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    shared_post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = db.Column(db.DateTime)

    author = db.relationship("User", foreign_keys=[author_user_id])
    shared_post = db.relationship("FeedPost", remote_side=[id], foreign_keys=[shared_post_id])
    attachments = db.relationship("FeedAttachment", backref="post", order_by="FeedAttachment.id")

    __table_args__ = (
        CheckConstraint("status IN ('PUBLISHED','DELETED','REMOVED')", name="ck_post_status"),
        db.Index("ix_post_group_created", "group_id", "created_at"),
        db.Index("ix_post_group_pinned", "group_id", "is_pinned"),
    )


class FeedPostLike(db.Model):
    __tablename__ = "feed_post_likes"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User")

    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_post_like"),)


class FeedPostComment(db.Model):
    __tablename__ = "feed_post_comments"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"), nullable=False, index=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = db.Column(db.DateTime)

    author = db.relationship("User")
    replies = db.relationship("FeedCommentReply", backref="comment", order_by="FeedCommentReply.id")


class FeedCommentReply(db.Model):
    """One level of threading: replies attach to a top-level comment. A reply
    to a reply stays on the same comment and records who it answers."""
    __tablename__ = "feed_comment_replies"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    comment_id = db.Column(db.Integer, db.ForeignKey("feed_post_comments.id"), nullable=False, index=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reply_to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reply_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = db.Column(db.DateTime)

    author = db.relationship("User", foreign_keys=[author_user_id])
    reply_to = db.relationship("User", foreign_keys=[reply_to_user_id])


class FeedAttachment(db.Model):
    __tablename__ = "feed_attachments"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"), nullable=False, index=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"), nullable=False)
    file_name = db.Column(db.String(200), nullable=False)
    file_type = db.Column(db.String(80), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    file = db.relationship("StoredFile")

    @property
    def is_image(self):
        return self.file_type.startswith("image/")


class FeedPostShare(db.Model):
    __tablename__ = "feed_post_shares"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"), nullable=False, index=True)
    shared_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    new_post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class FeedReport(db.Model):
    __tablename__ = "feed_reports"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    reported_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("feed_posts.id"))
    comment_id = db.Column(db.Integer, db.ForeignKey("feed_post_comments.id"))
    reply_id = db.Column(db.Integer, db.ForeignKey("feed_comment_replies.id"))
    # "post:12" / "comment:3" / "reply:9" - lets one unique constraint stop duplicate reports
    target_key = db.Column(db.String(30), nullable=False)
    reason = db.Column(db.String(40), nullable=False)
    description = db.Column(db.String(500))
    status = db.Column(db.String(12), nullable=False, default="OPEN")
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    reporter = db.relationship("User", foreign_keys=[reported_by])

    __table_args__ = (
        UniqueConstraint("reported_by", "target_key", name="uq_report_once"),
        CheckConstraint("status IN ('OPEN','DISMISSED','ACTIONED')", name="ck_report_status"),
    )


class FeedModerationLog(db.Model):
    __tablename__ = "feed_moderation_logs"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    moderator_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_type = db.Column(db.String(12), nullable=False)
    target_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    moderator = db.relationship("User")

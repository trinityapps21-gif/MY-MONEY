import json

from app.extensions import db
from app.models import utcnow


class ConsentForm(db.Model):
    __tablename__ = "consent_forms"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    intro = db.Column(db.Text)
    token = db.Column(db.String(40), nullable=False, unique=True, index=True)
    is_open = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    fields = db.relationship("ConsentField", backref="form", cascade="all, delete-orphan",
                             order_by="ConsentField.position, ConsentField.id")


class ConsentField(db.Model):
    __tablename__ = "consent_fields"

    id = db.Column(db.Integer, primary_key=True)
    form_id = db.Column(db.Integer, db.ForeignKey("consent_forms.id"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    label = db.Column(db.String(300), nullable=False)
    field_type = db.Column(db.String(20), nullable=False, default="short_text")
    options = db.Column(db.Text)  # one choice per line, for choice types
    required = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def option_list(self):
        return [o.strip() for o in (self.options or "").splitlines() if o.strip()]


class ConsentResponse(db.Model):
    __tablename__ = "consent_responses"

    id = db.Column(db.Integer, primary_key=True)
    form_id = db.Column(db.Integer, db.ForeignKey("consent_forms.id"), nullable=False, index=True)
    token = db.Column(db.String(40), nullable=False, unique=True, index=True)
    reference = db.Column(db.String(30), nullable=False)
    respondent = db.Column(db.String(160))
    answers_json = db.Column(db.Text, nullable=False)  # snapshot: [{"label","type","value"}]
    submitted_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    form = db.relationship("ConsentForm", backref=db.backref("responses", cascade="all, delete-orphan"))

    @property
    def answers(self):
        try:
            return json.loads(self.answers_json or "[]")
        except ValueError:
            return []

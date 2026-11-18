"""The write path of the definition store (WP-18): intent files and proposal facts.

`audit_event.files`: for an intent row, the SHA-256 of each file the action writes (or `null` for
a file it deletes), `{path: hash | null}`; a row whose `commit_sha` is still empty is an open
intent, and `makna index sync` matches it with a commit by these hashes (ARCHITECTURE section 10).
The unique constraint on `(commit_sha, object_id, action)` does not block a second intent,
because two empty commit ids are never equal in PostgreSQL.

`audit_event.failed_at`: when an intent that no commit completed was given up (git failed, or no
commit matched it). Such a row is kept, with an empty `commit_sha`, so an attempt leaves a trace.

`semantic_object.proposal_origin` and `proposal_errors`: where a proposal came from (`external:`
and the author's email for a change made outside the application) and the definition errors it
has, so that a review page can list them without validating again. Both are rebuilt by
`makna index build`.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

TIMESTAMPTZ = pg.TIMESTAMP(timezone=True)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_event", sa.Column("files", pg.JSONB))
    op.add_column("audit_event", sa.Column("failed_at", TIMESTAMPTZ))
    op.add_column("semantic_object", sa.Column("proposal_origin", sa.Text))
    op.add_column("semantic_object", sa.Column("proposal_errors", pg.JSONB))


def downgrade() -> None:
    op.drop_column("semantic_object", "proposal_errors")
    op.drop_column("semantic_object", "proposal_origin")
    op.drop_column("audit_event", "failed_at")
    op.drop_column("audit_event", "files")

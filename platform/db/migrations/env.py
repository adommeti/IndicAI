import os

from alembic import context
from indic_platform.config import settings as _settings  # noqa: F401
from indic_platform.db.models import Base
from sqlalchemy import create_engine, pool

url = os.getenv("DATABASE_URL", "postgresql+psycopg://platform@localhost:5433/platform")
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()

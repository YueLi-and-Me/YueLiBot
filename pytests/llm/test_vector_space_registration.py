"""向量空间登记以隔离数据库验证首次接管、换空间与事务回滚。"""

import sqlite3

import pytest


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE facts (id INTEGER, embedding BLOB, embedding_q8 BLOB)')
    db.execute('CREATE TABLE knowledge (id INTEGER, embedding BLOB, embedding_q8 BLOB)')
    db.execute('CREATE TABLE emoji (id INTEGER, emotion_vec BLOB)')
    db.execute("INSERT INTO facts VALUES (1, X'01', X'02')")
    db.execute("INSERT INTO knowledge VALUES (1, X'01', X'02')")
    db.execute("INSERT INTO emoji VALUES (1, X'01')")
    from src.core.db.migrations.v32_to_v33 import migrate
    migrate(db)
    db.commit()
    return db


@pytest.mark.parametrize('consumer,column', [('facts', 'embedding'), ('knowledge', 'embedding'), ('emoji', 'emotion_vec')])
def test_vector_space_first_then_unchanged_then_changed(consumer, column):
    from src.core.memory.vector_space import VectorSpace, reconcile_space
    db = database()
    space = VectorSpace('model', 'openai', 2, 'tags' if consumer == 'emoji' else 'text')
    assert reconcile_space(db, consumer, space) == 0
    assert db.execute(f'SELECT {column} FROM {consumer}').fetchone()[0] == b'\x01'
    changes = db.total_changes
    assert reconcile_space(db, consumer, space) == 0
    assert db.total_changes == changes
    assert reconcile_space(db, consumer, VectorSpace('other', 'openai', 2, space.recipe)) == 1
    assert db.execute(f'SELECT {column} FROM {consumer}').fetchone()[0] is None
    if consumer != 'emoji':
        assert db.execute(f'SELECT embedding_q8 FROM {consumer}').fetchone()[0] is None
    assert db.execute('SELECT model FROM vector_space WHERE consumer=?', (consumer,)).fetchone()[0] == 'other'
    db.close()


def test_migration_replay_and_chain_head(db):
    from src.core.db.migrations.manager import CURRENT_VERSION, load_migration_registry, run_migrations
    from src.core.db.migrations.v32_to_v33 import FROM_VERSION, migrate
    db.execute('DROP TABLE IF EXISTS vector_space')
    migrate(db)
    db.execute("INSERT INTO vector_space VALUES ('emoji','m','openai',2,'tags',1)")
    migrate(db)
    assert db.execute('SELECT count(*) FROM vector_space').fetchone()[0] == 1
    assert FROM_VERSION in load_migration_registry()
    run_migrations(db)
    assert db.execute('PRAGMA user_version').fetchone()[0] == max(load_migration_registry()) + 1 == CURRENT_VERSION


def test_migration_from_reserved_version_reaches_chain_head(db):
    from src.core.db.migrations.bootstrap import write_user_version
    from src.core.db.migrations.manager import CURRENT_VERSION, load_migration_registry, run_migrations
    from src.core.db.migrations.v32_to_v33 import FROM_VERSION
    db.execute('DROP TABLE vector_space')
    write_user_version(db, FROM_VERSION)
    db.commit()
    run_migrations(db)
    assert db.execute('PRAGMA user_version').fetchone()[0] == max(load_migration_registry()) + 1 == CURRENT_VERSION
    assert db.execute('SELECT count(*) FROM vector_space').fetchone()[0] == 0


def test_space_reset_and_registration_are_atomic():
    from src.core.memory.vector_space import VectorSpace, reconcile_space
    db = database()
    reconcile_space(db, 'emoji', VectorSpace('old', 'openai', 2, 'tags'))
    db.execute("CREATE TRIGGER reject_space BEFORE UPDATE ON vector_space BEGIN SELECT RAISE(ABORT, 'test rejection'); END")
    with pytest.raises(sqlite3.IntegrityError):
        reconcile_space(db, 'emoji', VectorSpace('new', 'openai', 2, 'tags'))
    assert db.execute('SELECT emotion_vec FROM emoji').fetchone()[0] == b'\x01'
    assert db.execute('SELECT model FROM vector_space').fetchone()[0] == 'old'
    db.close()


def test_same_dimension_different_model_rejected():
    from src.core.config.loader import _validate_vector_space
    from src.core.config.schema import ModelCandidate, TaskRouting
    routing = TaskRouting(task='embedding', candidates=[
        ModelCandidate(name='模型甲', provider='p', identifier='a', embedding_dim=2),
        ModelCandidate(name='模型乙', provider='p', identifier='b', embedding_dim=2),
    ])
    with pytest.raises(ValueError) as exc:
        _validate_vector_space(routing)
    assert all(value in str(exc.value) for value in ('embedding', '模型甲', '模型乙'))

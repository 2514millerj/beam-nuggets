from __future__ import division, print_function

import datetime

from sqlalchemy import (
    create_engine, MetaData, Table, Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Date,
    insert as generic_insert
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils import create_database, database_exists


class SourceConfiguration(object):
    def __init__(
        self,
        drivername,
        host=None,
        port=None,
        database=None,
        username=None,
        password=None,
        create_if_missing=False,
    ):
        self.url = URL(
            drivername=drivername,
            username=username,
            password=password,
            host=host,
            port=port,
            database=database
        )
        self.create_if_missing = create_if_missing


class TableConfiguration(object):
    def __init__(
        self,
        name,
        define_table_f=None,
        create_if_missing=False,
        primary_key_columns=None,
        create_insert_f=None
    ):
        """

        Args:
            name:
            define_table_f:
                https://docs.sqlalchemy.org/en/latest/core/metadata.html
                https://docs.sqlalchemy.org/en/latest/orm/extensions
                /declarative/table_config.html
                https://docs.sqlalchemy.org/en/latest/core/tutorial.html
                #define-and-create-tables
            create_if_missing:
            primary_key_columns:
        """
        self.name = name
        self.define_table_f = define_table_f
        self.create_table_if_missing = create_if_missing
        self.primary_key_column_names = primary_key_columns or []
        self.create_insert_f = create_insert_f


class SqlAlchemyDB(object):
    """
    TDOD
    """

    def __init__(self, source_config):
        """
        Args:
            source_config (SourceConfiguration):
        """
        self._source = source_config

        self._SessionClass = sessionmaker(bind=create_engine(self._source.url))
        self._session = None  # will be set in self.start_session()

        self._name_to_table = {}  # tables metadata cache

    def start_session(self):
        create_if_missing = self._source.create_if_missing
        database_is_missing = lambda: not database_exists(self._source.url)
        if create_if_missing and database_is_missing():
            create_database(self._source.url)
        self._session = self._SessionClass()

    def close_session(self):
        self._session.close()
        self._session = None

    def read(self, table_name):
        table = self._open_table_for_read(table_name)
        for record in table.records(self._session):
            yield record

    def write_record(self, table_config, record_dict):
        """
        https://docs.sqlalchemy.org/en/latest/dialects/postgresql.html
        #insert-on-conflict-upsert
        https://docs.sqlalchemy.org/en/latest/dialects/mysql.html#mysql
        -insert-on-duplicate-key-update
        """
        table = self._open_table_for_write(table_config, record_dict)
        table.write_record(
            session=self._session,
            create_insert_f=self._get_create_insert_f(table_config),
            record_dict=record_dict
        )

    def _get_create_insert_f(self, table_config):
        create_insert_f = table_config.create_insert_f
        if not create_insert_f:
            if 'postgresql' in self._source.url.drivername:
                create_insert_f = create_upsert_postgres
            elif 'mysql' in self._source.url.drivername:
                create_insert_f = create_upsert_mysql
            else:
                create_insert_f = create_insert
        return create_insert_f

    def _open_table_for_read(self, name):
        return self._open_table(
            name=name,
            get_table_f=load_table
        )

    def _open_table_for_write(self, table_config, record):
        return self._open_table(
            name=table_config.name,
            get_table_f=create_table,
            table_config=table_config,
            record=record
        )

    def _open_table(self, name, get_table_f, **get_table_f_params):
        table = self._name_to_table.get(name, None)
        if not table:
            self._name_to_table[name] = (
                self._get_table(name, get_table_f, **get_table_f_params)
            )
            table = self._name_to_table[name]
        return table

    def _get_table(self, name, get_table_f, **get_table_f_params):
        table_class = get_table_f(self._session, name, **get_table_f_params)
        if table_class:
            table = _Table(table_class=table_class, name=name)
        else:
            raise SqlAlchemyDbException('Failed to get table {}'.format(name))
        return table


class SqlAlchemyDbException(Exception):
    pass


class _Table(object):
    def __init__(self, table_class, name):
        self._Class = table_class
        self._sqlalchemy_table = table_class.__table__
        self.name = name
        self._column_names = get_column_names_from_table(table_class)

    def records(self, session):
        for record in session.query(self._Class):
            yield self._from_db_record(record)

    def write_record(self, session, create_insert_f, record_dict):
        try:
            insert_stmt = create_insert_f(
                table=self._sqlalchemy_table,
                record=record_dict
            )
            session.execute(insert_stmt)
            session.commit()
        except:
            session.rollback()
            session.close()
            raise

    def _to_db_record(self, record_dict):
        return self._Class(**record_dict)

    def _from_db_record(self, db_record):
        return {col: getattr(db_record, col) for col in self._column_names}


def load_table(session, name):
    table_class = None
    engine = session.bind
    if engine.dialect.has_table(engine, name):
        metadata = MetaData(bind=engine)
        table_class = create_table_class(Table(name, metadata, autoload=True))
    return table_class


def create_table(session, name, table_config, record):
    # Attempt to load from the DB
    table_class = load_table(session, name)

    if not table_class and table_config.create_table_if_missing:
        define_table_f = (
            table_config.define_table_f or
            _get_default_define_f(
                record=record,
                name=name,
                primary_key_column_names=table_config.primary_key_column_names,
                drivername=session.bind.url.drivername,
            )
        )
        metadata = MetaData(bind=session.bind)
        sqlalchemy_table = define_table_f(metadata)
        metadata.create_all()
        table_class = create_table_class(sqlalchemy_table)

    return table_class


def create_table_class(sqlalchemy_table):
    class TableClass(declarative_base()):
        __table__ = sqlalchemy_table

    return TableClass


def _get_default_define_f(record, name, primary_key_column_names, drivername):
    def define_table(metadata):
        columns = _columns_from_sample_record(
            record=record,
            primary_key_column_names=primary_key_column_names,
            drivername=drivername
        )
        return Table(name, metadata, *columns)

    return define_table


def _columns_from_sample_record(record, primary_key_column_names, drivername):
    if len(primary_key_column_names) > 0:
        primary_key_columns = [
            Column(
                col, infer_db_type(record[col], drivername), primary_key=True
            )
            for col in primary_key_column_names
        ]
        other_columns = [
            Column(col, infer_db_type(value, drivername))
            for col, value in record.iteritems()
            if col not in primary_key_column_names
        ]
    else:
        pri_col_name = 'id'
        while pri_col_name in record.keys():
            pri_col_name += '_'
        primary_key_columns = [Column(pri_col_name, Integer, primary_key=True)]
        other_columns = [
            Column(col, infer_db_type(value, drivername))
            for col, value in record.iteritems()
        ]
    return primary_key_columns + other_columns


def create_insert(table, record):
    """
    https://docs.sqlalchemy.org/en/latest/core/dml.html
    https://docs.sqlalchemy.org/en/latest/core/tutorial.html#insert-expressions
    """
    return generic_insert(table).values(record)


def create_upsert_postgres(table, record):
    """
    https://docs.sqlalchemy.org/en/latest/dialects/postgresql.html#insert-on
    -conflict-upsert
    """
    insert_stmt = postgres_insert(table).values(record)
    return insert_stmt.on_conflict_do_update(
        index_elements=[col for col in table.primary_key],
        set_=record
    )


def create_upsert_mysql(table, record):
    """
    https://docs.sqlalchemy.org/en/latest/dialects/mysql.html#mysql-insert
    -on-duplicate-key-update
    """
    insert_stmt = mysql_insert(table).values(record)
    return insert_stmt.on_duplicate_key_update(**record)
    # passing dict, i.e. ...update(record), isn't working


def get_column_names_from_table(table_class):
    return [col.name for col in table_class.__table__.columns]


def infer_db_type(val, drivername):
    for is_type_f, db_type in PYTHON_TO_DB_TYPE:
        if is_type_f(val):
            return db_type
    return String if _does_support_varchar(drivername) else String(100)
    # FIXME: Users familiar with the syntax of CREATE TABLE may notice
    #  that the VARCHAR columns were generated without a length; on
    #  SQLite and PostgreSQL, this is a valid datatype, but on others,
    #  it\'s not allowed.


def _does_support_varchar(drivername):
    return 'postgresql' in drivername or 'sqlite' in drivername


def _is_number(x):
    try:
        _ = x + 1
    except:
        return False
    return not hasattr(x, '__len__')


PYTHON_TO_DB_TYPE = [
    # Order matters!
    (lambda x: isinstance(x, bool), Boolean),
    (_is_number, Float),
    (lambda x: isinstance(x, datetime.datetime), DateTime),
    (lambda x: isinstance(x, datetime.date), Date),
]

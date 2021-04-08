#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Simple read-only API for Things.

We attempt to follow the names used in the Things app, URL Scheme,
and SQL database whenever possible.
"""

from __future__ import print_function

__author__ = "Alexander Willner"
__copyright__ = "2020 Alexander Willner"
__credits__ = ["Alexander Willner"]
__license__ = "Apache License 2.0"
__version__ = "0.0.7"
__maintainer__ = "Alexander Willner"
__email__ = "alex@willner.ws"
__status__ = "Development"

import datetime
import os
import plistlib
import sqlite3
import sys


DEFAULT_DATABASE_FILEPATH = os.path.expanduser(
    "~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac"
    "/Things Database.thingsdatabase/main.sqlite"
)

START_TO_FILTER = {
    "Inbox": "start = 0",
    "Anytime": "start = 1",
    "Someday": "start = 2",
}

STATUS_TO_FILTER = {
    "incomplete": "status = 0",
    "canceled": "status = 2",
    "completed": "status = 3",
}

TYPE_TO_FILTER = {"to-do": "type = 0", "project": "type = 1", "heading": "type = 2"}

INDICES = ("index", "todayIndex")

COLUMNS_TO_OMIT_IF_NONE = ("area", "area_title", "project", "project_title",
                           "heading", "heading_title", "trashed", "checklist", "tags")
COLUMNS_TO_TRANSFORM_TO_BOOL = ("trashed", "checklist", "tags")


# pylint: disable=R0904,R0902
class Database:
    """
    Access Things SQL database.

    Parameters
    ----------
    filepath : str, optional
        Any valid path of a SQLite database file generated by the Things app.
        If no path is provided, then access the default database path.
    """

    # Database info
    TABLE_TASK = "TMTask"
    TABLE_AREA = "TMArea"
    TABLE_TAG = "TMTag"
    TABLE_TASKTAG = "TMTaskTag"
    TABLE_AREATAG = "TMAreaTag"
    TABLE_CHECKLIST_ITEM = "TMChecklistItem"
    TABLE_META = "Meta"
    DATE_CREATE = "creationDate"
    DATE_MOD = "userModificationDate"
    DATE_DEADLINE = "dueDate"
    DATE_START = "startDate"
    DATE_STOP = "stopDate"
    IS_INBOX = START_TO_FILTER["Inbox"]
    IS_ANYTIME = START_TO_FILTER["Anytime"]
    IS_SOMEDAY = START_TO_FILTER["Someday"]
    IS_SCHEDULED = f"{DATE_START} IS NOT NULL"
    IS_NOT_SCHEDULED = f"{DATE_START} IS NULL"
    IS_DEADLINE = f"{DATE_DEADLINE} IS NOT NULL"
    IS_RECURRING = "recurrenceRule IS NOT NULL"
    IS_NOT_RECURRING = "recurrenceRule IS NULL"
    IS_TODO = TYPE_TO_FILTER["to-do"]
    IS_PROJECT = TYPE_TO_FILTER["project"]
    IS_HEADING = TYPE_TO_FILTER["heading"]
    IS_NOT_TRASHED = "trashed = 0"
    IS_TRASHED = "trashed = 1"
    IS_INCOMPLETE = STATUS_TO_FILTER["incomplete"]
    IS_CANCELED = STATUS_TO_FILTER["canceled"]
    IS_COMPLETED = STATUS_TO_FILTER["completed"]
    RECURRING_IS_NOT_PAUSED = "instanceCreationPaused = 0"
    RECURRING_HAS_NEXT_STARTDATE = "nextInstanceStartDate IS NOT NULL"

    # Variables
    debug = False
    filter = ""

    # pylint: disable=R0913
    def __init__(self, filepath=None):
        self.filepath = filepath or os.environ.get("THINGSDB") or DEFAULT_DATABASE_FILEPATH

        # Automated migration to new database location in Things 3.12.6/3.13.1
        # --------------------------------
        try:
            with open(self.filepath) as file:
                if "Your database file has been moved there" in file.readline():
                    self.filepath = DEFAULT_DATABASE_FILEPATH
        except (UnicodeDecodeError, FileNotFoundError, PermissionError):
            pass  # binary file (old database) or doesn't exist
        # --------------------------------

    # Core methods

    def get_tasks(self,  # pylint: disable=R0914
                  uuid=None,
                  type=None,  # pylint: disable=W0622
                  status=None,
                  start=None,
                  area=None,
                  project=None,
                  heading=None,
                  tag=None,
                  start_date=None,
                  deadline=None,
                  index="index",
                  count_only=False,
                  search_query=None):
        """Get tasks. See `api.tasks` for details on parameters."""

        # Overwrites
        start = start and start.title()

        # Validation
        validate("type", type, [None] + list(TYPE_TO_FILTER))  # type: ignore
        validate("status", status, [None] + list(STATUS_TO_FILTER))  # type: ignore
        validate("start", start, [None] + list(START_TO_FILTER))  # type: ignore
        validate("index", index, list(INDICES))

        if tag is not None:
            valid_tags = self.get_tags(titles_only=True)
            validate("tag", tag, [None] + list(valid_tags))

        if uuid:
            if count_only is False and not self.get_tasks(uuid=uuid, count_only=True):
                raise ValueError(f"No such task uuid found: {uuid!r}")

        # Query
        # TK: might consider executing SQL with parameters instead.
        # See: https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.execute

        if uuid:
            where_predicate = f"TRUE {make_filter('TASK.uuid', uuid)}"
        else:
            start_filter = START_TO_FILTER.get(start, "")
            status_filter = STATUS_TO_FILTER.get(status, "")
            type_filter = TYPE_TO_FILTER.get(type, "")

            where_predicate = f"""
                    TASK.{self.IS_NOT_TRASHED}
                    {type_filter and f"AND TASK.{type_filter}"}
                    {start_filter and f"AND TASK.{start_filter}"}
                    {status_filter and f"AND TASK.{status_filter}"}
                    AND TASK.{self.IS_NOT_RECURRING}
                    AND (PROJECT.title IS NULL OR PROJECT.{self.IS_NOT_TRASHED})
                    AND (HEADPROJ.title IS NULL OR HEADPROJ.{self.IS_NOT_TRASHED})
                    {make_filter('TASK.uuid', uuid)}
                    {make_filter("TASK.area", area)}
                    {make_filter("TASK.project", project)}
                    {make_filter("TASK.actionGroup", heading)}
                    {make_filter("TASK.startDate", start_date)}
                    {make_filter(f"TASK.{self.DATE_DEADLINE}", deadline)}
                    {make_filter("TAG.title", tag)}
                    {make_search_filter(search_query)}
                    ORDER BY TASK."{index}"
                    """

        sql_query = self.make_task_sql_query(where_predicate)
        if count_only:
            return self.get_count(sql_query)

        return self.execute_query(sql_query)

    def make_task_sql_query(self, where_predicate):
        """Make SQL query for Task table"""
        return f"""
            SELECT DISTINCT
                TASK.uuid,
                CASE
                    WHEN TASK.{self.IS_TODO} THEN 'to-do'
                    WHEN TASK.{self.IS_PROJECT} THEN 'project'
                    WHEN TASK.{self.IS_HEADING} THEN 'heading'
                END AS type,
                CASE
                    WHEN TASK.{self.IS_TRASHED} THEN 1
                END AS trashed,
                TASK.title,
                CASE
                    WHEN TASK.{self.IS_INCOMPLETE} THEN 'incomplete'
                    WHEN TASK.{self.IS_COMPLETED} THEN 'completed'
                    WHEN TASK.{self.IS_CANCELED} THEN 'canceled'
                END AS status,
                CASE
                    WHEN AREA.uuid IS NOT NULL THEN AREA.uuid
                END AS area,
                CASE
                    WHEN AREA.uuid IS NOT NULL THEN AREA.title
                END AS area_title,
                CASE
                    WHEN PROJECT.uuid IS NOT NULL THEN PROJECT.uuid
                END AS project,
                CASE
                    WHEN PROJECT.uuid IS NOT NULL THEN PROJECT.title
                END AS project_title,
                CASE
                    WHEN HEADING.uuid IS NOT NULL THEN HEADING.uuid
                END AS heading,
                CASE
                    WHEN HEADING.uuid IS NOT NULL THEN HEADING.title
                END AS heading_title,
                TASK.notes,
                CASE
                    WHEN TAG.uuid IS NOT NULL THEN 1
                END AS tags,
                CASE
                    WHEN TASK.{self.IS_INBOX} THEN 'Inbox'
                    WHEN TASK.{self.IS_ANYTIME} THEN 'Anytime'
                    WHEN TASK.{self.IS_SOMEDAY} THEN 'Someday'
                END AS start,
                CASE
                    WHEN CHECKLIST_ITEM.uuid IS NOT NULL THEN 1
                END AS checklist,
                date(TASK.startDate, "unixepoch") AS start_date,
                date(TASK.{self.DATE_DEADLINE}, "unixepoch") AS deadline,
                date(TASK.stopDate, "unixepoch") AS "stop_date",
                datetime(TASK.{self.DATE_CREATE}, "unixepoch", "localtime") AS created,
                datetime(TASK.{self.DATE_MOD}, "unixepoch", "localtime") AS modified,
                TASK.'index',
                TASK.todayIndex
            FROM
                {self.TABLE_TASK} AS TASK
            LEFT OUTER JOIN
                {self.TABLE_TASK} PROJECT ON TASK.project = PROJECT.uuid
            LEFT OUTER JOIN
                {self.TABLE_AREA} AREA ON TASK.area = AREA.uuid
            LEFT OUTER JOIN
                {self.TABLE_TASK} HEADING ON TASK.actionGroup = HEADING.uuid
            LEFT OUTER JOIN
                {self.TABLE_TASK} HEADPROJ ON HEADING.project = HEADPROJ.uuid
            LEFT OUTER JOIN
                {self.TABLE_TASKTAG} TAGS ON TASK.uuid = TAGS.tasks
            LEFT OUTER JOIN
                {self.TABLE_TAG} TAG ON TAGS.tags = TAG.uuid
            LEFT OUTER JOIN
                {self.TABLE_CHECKLIST_ITEM} CHECKLIST_ITEM
                ON CHECKLIST_ITEM.task = TASK.uuid
            WHERE
                {self.filter}
                {where_predicate}
            """

    def get_task_rows(self, where_predicate):
        """Executes SQL query with given WHERE clauses."""
        return self.execute_query(self.make_task_sql_query(where_predicate))

    def get_areas(self, uuid=None, tag=None, count_only=False):
        """Get areas. See `api.areas` for details on parameters."""

        # Validation
        if tag is not None:
            valid_tags = self.get_tags(titles_only=True)
            validate("tag", tag, [None] + list(valid_tags))

        if (uuid and count_only is False and not self.get_areas(uuid=uuid, count_only=True)):
            raise ValueError(f"No such area uuid found: {uuid!r}")

        # Query
        sql_query = f"""
                SELECT
                    DISTINCT AREA.uuid,
                    'area' as type,
                    AREA.title,
                    CASE
                        WHEN AREA_TAG.areas IS NOT NULL THEN 1
                    END AS tags
                FROM
                    {self.TABLE_AREA} AS AREA
                LEFT OUTER JOIN
                    {self.TABLE_AREATAG} AREA_TAG ON AREA_TAG.areas = AREA.uuid
                LEFT OUTER JOIN
                    {self.TABLE_TAG} TAG ON TAG.uuid = AREA_TAG.tags
                WHERE
                    TRUE
                    {make_filter('TAG.title', tag)}
                    {make_filter('AREA.uuid', uuid)}
                ORDER BY AREA."index"
                """
        if count_only:
            return self.get_count(sql_query)

        return self.execute_query(sql_query)

    def get_checklist_items(self, task_uuid=None):
        """Get checklist items."""
        sql_query = f"""
                SELECT
                    CHECKLIST_ITEM.title,
                    CASE
                        WHEN CHECKLIST_ITEM.{self.IS_INCOMPLETE} THEN 'incomplete'
                        WHEN CHECKLIST_ITEM.{self.IS_COMPLETED} THEN 'completed'
                        WHEN CHECKLIST_ITEM.{self.IS_CANCELED} THEN 'canceled'
                    END AS status,
                    date(CHECKLIST_ITEM.stopDate, "unixepoch") AS stop_date,
                    'checklist-item' as type,
                    CHECKLIST_ITEM.uuid,
                    datetime(
                        CHECKLIST_ITEM.{self.DATE_MOD}, "unixepoch", "localtime"
                    ) AS created,
                    datetime(
                        CHECKLIST_ITEM.{self.DATE_MOD}, "unixepoch", "localtime"
                    ) AS modified
                FROM
                    {self.TABLE_CHECKLIST_ITEM} AS CHECKLIST_ITEM
                WHERE
                    CHECKLIST_ITEM.task = ?
                ORDER BY CHECKLIST_ITEM."index"
                """
        return self.execute_query(sql_query, (task_uuid,))

    def get_tags(self, title=None, area=None, task=None, titles_only=False):
        """Get tags. See `api.tags` for details on parameters."""

        # Validation
        if title is not None:
            valid_titles = self.get_tags(titles_only=True)
            validate("title", title, [None] + list(valid_titles))

        # Query
        if task:
            return self.get_tags_of_task(task)
        if area:
            return self.get_tags_of_area(area)

        if titles_only:
            sql_query = f'SELECT title FROM {self.TABLE_TAG} ORDER BY "index"'
            return self.execute_query(sql_query, row_factory=list_factory)

        sql_query = f"""
            SELECT
                uuid,
                'tag' AS type,
                title,
                shortcut
            FROM
                {self.TABLE_TAG}
            WHERE
                TRUE
                {make_filter('title', title)}
            ORDER BY "index"
            """
        return self.execute_query(sql_query)

    def get_tags_of_task(self, task_uuid):
        """Get tag titles of task"""
        sql_query = f"""
            SELECT
                TAG.title
            FROM
                {self.TABLE_TASKTAG} AS TASK_TAG
            LEFT OUTER JOIN
                {self.TABLE_TAG} TAG ON TAG.uuid = TASK_TAG.tags
            WHERE
                TASK_TAG.tasks = ?
            ORDER BY TAG."index"
            """
        return self.execute_query(
            sql_query, parameters=(task_uuid,), row_factory=list_factory
        )

    def get_tags_of_area(self, area_uuid):
        """Get tag titles for area"""
        sql_query = f"""
            SELECT
                AREA.title
            FROM
                {self.TABLE_AREATAG} AS AREA_TAG
            LEFT OUTER JOIN
                {self.TABLE_TAG} AREA ON AREA.uuid = AREA_TAG.tags
            WHERE
                AREA_TAG.areas = ?
            ORDER BY AREA."index"
            """
        return self.execute_query(
            sql_query, parameters=(area_uuid,), row_factory=list_factory
        )

    def get_version(self):
        """Get Things Database version."""

        sql_query = f"SELECT value FROM {self.TABLE_META} WHERE key = 'databaseVersion'"
        result = self.execute_query(sql_query, row_factory=list_factory)
        plist_bytes = result[0].encode()
        return plistlib.loads(plist_bytes)

    def get_count(self, sql):
        """Count number of results."""
        sql_query = f"""SELECT COUNT(uuid) FROM ({sql})"""
        return self.execute_query(sql_query, row_factory=list_factory)[0]

    # noqa todo: add type hinting for resutl (List[Tuple[str, Any]]?)
    def execute_query(self, sql_query, parameters=(), row_factory=None):
        """Run the actual SQL query"""
        if self.debug is True:
            print(self.filepath)
            print(sql_query)
        try:
            uri = f"file:{self.filepath}?mode=ro"  # "ro" means read-only
            connection = sqlite3.connect(uri, uri=True)  # pylint: disable=E1101
            connection.row_factory = row_factory or dict_factory
            cursor = connection.cursor()
            cursor.execute(sql_query, parameters)
            tasks = cursor.fetchall()
            if self.debug:
                for task in tasks:
                    print(task)
            return tasks
        except sqlite3.OperationalError as error:  # pylint: disable=E1101
            print(f"Could not query the database at: {self.filepath}.")
            print(f"Details: {error}.")
            sys.exit(2)

    # -------- Utility methods --------

    def last_modified(self):
        """Get last modified time."""
        mtime_seconds = os.path.getmtime(self.filepath)
        return datetime.datetime.fromtimestamp(mtime_seconds)

    def was_modified_today(self):
        """Was task modified today?"""
        last_modified_date = self.last_modified().date()
        todays_date = datetime.datetime.now().date()
        return last_modified_date >= todays_date

    # -------- Historical methods (TK: transform) --------

    def get_trashed(self):
        """Get trashed tasks."""
        query = f"""
                TASK.{self.IS_TRASHED} AND
                TASK.{self.IS_TODO}
                ORDER BY TASK.{self.DATE_STOP}
                """
        return self.get_task_rows(query)

    def get_lint(self):
        """Get tasks that float around"""
        query = f"""
            TASK.{self.IS_NOT_TRASHED} AND
            TASK.{self.IS_INCOMPLETE} AND
            TASK.{self.IS_TODO} AND
            (TASK.{self.IS_SOMEDAY} OR TASK.{self.IS_ANYTIME}) AND
            TASK.project IS NULL AND
            TASK.area IS NULL AND
            TASK.actionGroup IS NULL
            """
        return self.get_task_rows(query)

    def get_empty_projects(self):
        """Get projects that are empty"""
        query = f"""
            TASK.{self.IS_NOT_TRASHED} AND
            TASK.{self.IS_INCOMPLETE} AND
            TASK.{self.IS_PROJECT} AND
            TASK.{self.IS_ANYTIME}
            GROUP BY TASK.uuid
            HAVING
                (SELECT COUNT(uuid)
                 FROM TMTask AS PROJECT_TASK
                 WHERE
                   PROJECT_TASK.project = TASK.uuid AND
                   PROJECT_TASK.{self.IS_NOT_TRASHED} AND
                   PROJECT_TASK.{self.IS_INCOMPLETE} AND
                   (PROJECT_TASK.{self.IS_ANYTIME} OR
                    PROJECT_TASK.{self.IS_SCHEDULED} OR
                      (PROJECT_TASK.{self.IS_RECURRING} AND
                       PROJECT_TASK.{self.RECURRING_IS_NOT_PAUSED} AND
                       PROJECT_TASK.{self.RECURRING_HAS_NEXT_STARTDATE}
                      )
                   )
                ) = 0
            """
        return self.get_task_rows(query)

    def get_largest_projects(self):
        """Get projects that are empty"""
        query = f"""
            SELECT
                TASK.uuid,
                TASK.title AS title,
                {self.DATE_CREATE} AS created,
                {self.DATE_MOD} AS modified,
                (SELECT COUNT(uuid)
                 FROM TMTask AS PROJECT_TASK
                 WHERE
                   PROJECT_TASK.project = TASK.uuid AND
                   PROJECT_TASK.{self.IS_NOT_TRASHED} AND
                   PROJECT_TASK.{self.IS_INCOMPLETE}
                ) AS tasks
            FROM
                {self.TABLE_TASK} AS TASK
            WHERE
               TASK.{self.IS_NOT_TRASHED} AND
               TASK.{self.IS_INCOMPLETE} AND
               TASK.{self.IS_PROJECT}
            GROUP BY TASK.uuid
            ORDER BY tasks COLLATE NOCASE DESC
            """
        return self.execute_query(query)

    def get_daystats(self):
        """Get a history of task activities

        TK: understand what this does and whether to keep.
        """
        stat_days = 365

        query = f"""
                WITH RECURSIVE timeseries(x) AS (
                    SELECT 0
                    UNION ALL
                    SELECT x+1 FROM timeseries
                    LIMIT {stat_days}
                )
                SELECT
                    date(julianday("now", "-{stat_days} days"),
                         "+" || x || " days") as date,
                    CREATED.TasksCreated as created,
                    CLOSED.TasksClosed as completed,
                    CANCELED.TasksCanceled as canceled,
                    TRASHED.TasksTrashed as trashed
                FROM timeseries
                LEFT JOIN
                    (SELECT COUNT(uuid) AS TasksCreated,
                        date({self.DATE_CREATE},"unixepoch") AS DAY
                        FROM {self.TABLE_TASK} AS TASK
                        WHERE DAY NOT NULL
                          AND TASK.{self.IS_TODO}
                        GROUP BY DAY)
                    AS CREATED ON CREATED.DAY = date
                LEFT JOIN
                    (SELECT COUNT(uuid) AS TasksCanceled,
                        date(stopDate,"unixepoch") AS DAY
                        FROM {self.TABLE_TASK} AS TASK
                        WHERE DAY NOT NULL
                          AND TASK.{self.IS_CANCELED} AND TASK.{self.IS_TODO}
                        GROUP BY DAY)
                        AS CANCELED ON CANCELED.DAY = date
                LEFT JOIN
                    (SELECT COUNT(uuid) AS TasksTrashed,
                        date({self.DATE_MOD},"unixepoch") AS DAY
                        FROM {self.TABLE_TASK} AS TASK
                        WHERE DAY NOT NULL
                          AND TASK.{self.IS_TRASHED} AND TASK.{self.IS_TODO}
                        GROUP BY DAY)
                        AS TRASHED ON TRASHED.DAY = date
                LEFT JOIN
                    (SELECT COUNT(uuid) AS TasksClosed,
                        date(stopDate,"unixepoch") AS DAY
                        FROM {self.TABLE_TASK} AS TASK
                        WHERE DAY NOT NULL
                          AND TASK.{self.IS_COMPLETED} AND TASK.{self.IS_TODO}
                        GROUP BY DAY)
                        AS CLOSED ON CLOSED.DAY = date
                """
        return self.execute_query(query)

    def get_minutes_today(self):
        """Count the planned minutes for today."""
        query = f"""
                SELECT
                    SUM(TAG.title) AS minutes
                FROM
                    {self.TABLE_TASK} AS TASK
                LEFT OUTER JOIN
                TMTask PROJECT ON TASK.project = PROJECT.uuid
                LEFT OUTER JOIN
                    TMArea AREA ON TASK.area = AREA.uuid
                LEFT OUTER JOIN
                    TMTask HEADING ON TASK.actionGroup = HEADING.uuid
                LEFT OUTER JOIN
                    TMTask HEADPROJ ON HEADING.project = HEADPROJ.uuid
                LEFT OUTER JOIN
                    TMTaskTag TAGS ON TASK.uuid = TAGS.tasks
                LEFT OUTER JOIN
                    TMTag TAG ON TAGS.tags = TAG.uuid
                WHERE
                    printf("%d", TAG.title) = TAG.title AND
                    TASK.{self.IS_NOT_TRASHED} AND
                    TASK.{self.IS_TODO} AND
                    TASK.{self.IS_INCOMPLETE} AND
                    TASK.{self.IS_ANYTIME} AND
                    TASK.{self.IS_SCHEDULED} AND (
                        (
                            PROJECT.title IS NULL OR (
                                PROJECT.{self.IS_NOT_TRASHED}
                            )
                        ) AND (
                            HEADPROJ.title IS NULL OR (
                                HEADPROJ.{self.IS_NOT_TRASHED}
                            )
                        )
                    )
                """
        return self.execute_query(query)

    def get_cleanup(self):
        """Tasks and projects that need work."""
        result = []
        result.extend(self.get_lint())
        result.extend(self.get_empty_projects())
        result = [i for n, i in enumerate(result) if i not in result[n + 1 :]]
        return result

    @staticmethod
    def get_not_implemented():
        """Not implemented warning."""
        return [{"title": "not implemented"}]

    functions = {
        "trashed": get_trashed,
        "areas": get_areas,
        "lint": get_lint,
        "empty": get_empty_projects,
        "cleanup": get_cleanup,
        "top-proj": get_largest_projects,
        "stats-day": get_daystats,
        "stats-min-today": get_minutes_today,
    }


# Helper functions


def dict_factory(cursor, row):
    """
    Convert SQL result into a dictionary.

    See also:
    https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.row_factory
    """
    result = {}
    for index, column in enumerate(cursor.description):
        key, value = column[0], row[index]
        if value is None and key in COLUMNS_TO_OMIT_IF_NONE:
            continue
        if value and key in COLUMNS_TO_TRANSFORM_TO_BOOL:
            value = bool(value)
        result[key] = value
    return result


def list_factory(_cursor, row):
    """
    Convert SQL selects of one column into a list.
    """
    return row[0]


def make_filter(column, value):
    """Filter SQL query by AND {column} = "{value}"."""
    default = f'AND {column} = "{value}"'
    # special options
    return {
        None: "",
        False: f"AND {column} IS NULL",
        True: f"AND {column} IS NOT NULL",
    }.get(value, default)


def make_search_filter(query: str) -> str:
    """
    Example
    -------
    >>> make_search_filter('dinner')
    'AND (
        TASK.title LIKE "%dinner%"
        OR TASK.notes LIKE "%dinner%"
        OR AREA.title LIKE "%dinner%"
    )'
    """
    if not query:
        return ""

    # noqa todo 'TMChecklistItem.title'
    columns = ["TASK.title", "TASK.notes", "AREA.title"]

    sub_searches = (f'{column} LIKE "%{query}%"' for column in columns)

    return f"AND ({' OR '.join(sub_searches)})"


def validate(parameter, argument, valid_arguments):
    """
    For a given parameter, check if its argument type is valid.
    If not, then raise ValueError.

    Example
    -------
    >>> validate(
        parameter='status',
        argument='completed',
        valid_arguments=['incomplete', 'completed']
    )
    """
    if argument in valid_arguments:
        return
    message = f"Unrecognized {parameter} type: {argument!r}"
    message += f"\nValid {parameter} types are {valid_arguments}"
    raise ValueError(message)

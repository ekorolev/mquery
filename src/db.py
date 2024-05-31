from collections import defaultdict
from contextlib import contextmanager
from typing import List, Optional, Dict, Any
from time import time
import random
import string
from redis import StrictRedis
from enum import Enum
from rq import Queue  # type: ignore
from sqlmodel import Session, SQLModel, create_engine, select, and_, update

from .models.agentgroup import AgentGroup
from .models.configentry import ConfigEntry
from .models.job import Job
from .models.jobagent import JobAgent
from .models.match import Match
from .schema import MatchesSchema, ConfigSchema
from .config import app_config


# "Magic" plugin name, used for configuration of mquery itself
MQUERY_PLUGIN_NAME = "Mquery"


class TaskType(Enum):
    SEARCH = "search"
    YARA = "yara"
    RELOAD = "reload"
    COMMAND = "command"


class AgentTask:
    def __init__(self, type: TaskType, data: str):
        self.type = type
        self.data = data


# Type alias for Job ids
JobId = str


class Database:
    def __init__(self, redis_host: str, redis_port: int) -> None:
        self.redis: Any = StrictRedis(
            host=redis_host, port=redis_port, decode_responses=True
        )
        self.engine = create_engine(app_config.database.url)

    def __schedule(self, agent: str, task: Any, *args: Any) -> None:
        """Schedules the task to agent group `agent` using rq."""
        Queue(agent, connection=self.redis).enqueue(
            task, *args, job_timeout=app_config.rq.job_timeout
        )

    @contextmanager
    def session(self):
        with Session(self.engine) as session:
            yield session
            session.commit()

    def get_job_ids(self, session: Session) -> List[JobId]:
        """Gets IDs of all jobs in the database."""
        jobs = session.exec(select(Job)).all()
        return [j.id for j in jobs]

    def cancel_job(self, session: Session, job: JobId, error=None) -> None:
        """Sets the job status to cancelled, with optional error message."""
        session.execute(
            update(Job)
            .where(Job.id == job)
            .values(status="cancelled", finished=int(time()), error=error)
        )

    def fail_job(self, session: Session, job: JobId, message: str) -> None:
        """Sets the job status to cancelled with provided error message."""
        self.cancel_job(session, job, message)

    def get_job(self, session: Session, job: JobId) -> Job:
        """Retrieves a job from the database."""
        return session.exec(select(Job).where(Job.id == job)).one()

    def remove_query(self, session: Session, job: JobId) -> None:
        """Sets the job status to removed."""
        session.execute(
            update(Job).where(Job.id == job).values(status="removed")
        )

    def add_match(self, session: Session, job: JobId, match: Match) -> None:
        job_object = self.get_job(session, job)
        match.job = job_object
        session.add(match)

    def job_contains(
        self, session: Session, job: JobId, ordinal: int, file_path: str
    ) -> bool:
        """Make sure that the file path is in the job results."""
        job_object = self.get_job(session, job)
        statement = select(Match).where(
            and_(Match.job == job_object, Match.file == file_path)
        )
        entry = session.exec(statement).one_or_none()
        return entry is not None

    def job_start_work(
        self, session: Session, job: JobId, in_progress: int
    ) -> None:
        """Updates the number of files being processed right now.
        :param job: ID of the job being updated.
        :param in_progress: Number of files in the current work unit.
        """
        session.execute(
            update(Job)
            .where(Job.id == job)
            .values(files_in_progress=Job.files_in_progress + in_progress)
        )

    def agent_finish_job(self, session: Session, job: Job) -> None:
        """Decrements the number of active agents in the given job. If there
        are no more agents, job status is changed to done.
        """
        (agents_left,) = session.execute(
            update(Job)
            .where(Job.internal_id == job.internal_id)
            .values(agents_left=Job.agents_left - 1)
            .returning(Job.agents_left)
        ).one()
        if agents_left == 0:
            session.execute(
                update(Job)
                .where(Job.internal_id == job.internal_id)
                .values(finished=int(time()), status="done")
            )

    def init_jobagent(
        self, session: Session, job: Job, agent_id: int, tasks: int
    ) -> None:
        """Creates a new JobAgent object.
        If tasks==0 then finishes job immediately"""
        obj = JobAgent(
            task_in_progress=tasks,
            job_id=job.internal_id,
            agent_id=agent_id,
        )
        session.add(obj)
        if tasks == 0:
            self.agent_finish_job(session, job)

    def add_tasks_in_progress(
        self, session: Session, job: Job, agent_id: int, tasks: int
    ) -> None:
        """Increments (or decrements, for negative values) the number of tasks
        that are in progress for agent. The number of tasks in progress should
        always stay positive for jobs in status inprogress. This function will
        automatically call agent_finish_job if the agent has no more tasks left.
        """
        (tasks_left,) = session.execute(
            update(JobAgent)
            .where(JobAgent.job_id == job.internal_id)
            .where(JobAgent.agent_id == agent_id)
            .values(task_in_progress=JobAgent.task_in_progress + tasks)
            .returning(JobAgent.task_in_progress)
        ).one()
        assert tasks_left >= 0
        if tasks_left == 0:
            self.agent_finish_job(session, job)

    def job_update_work(
        self,
        session: Session,
        job: JobId,
        processed: int,
        matched: int,
        errored: int,
    ) -> int:
        """Updates progress for the job. This will increment numbers processed,
        inprogress, errored and matched files.
        Returns the number of processed files after the operation.
        """
        (files_processed,) = session.execute(
            update(Job)
            .where(Job.id == job)
            .values(
                files_processed=Job.files_processed + processed,
                files_in_progress=Job.files_in_progress - processed,
                files_matched=Job.files_matched + matched,
                files_errored=Job.files_errored + errored,
            )
            .returning(Job.files_processed)
        ).one()
        return files_processed

    def init_job_datasets(
        self, session: Session, job: JobId, num_datasets: int
    ) -> None:
        """Sets total_datasets and datasets_left, and status to processing."""
        session.execute(
            update(Job)
            .where(Job.id == job)
            .values(
                total_datasets=num_datasets,
                datasets_left=num_datasets,
                status="processing",
            )
        )

    def dataset_query_done(self, session: Session, job: JobId):
        """Decrements the number of datasets left by one."""
        session.execute(
            update(Job)
            .where(Job.id == job)
            .values(datasets_left=Job.datasets_left - 1)
        )

    def create_search_task(
        self,
        session: Session,
        rule_name: str,
        rule_author: str,
        raw_yara: str,
        files_limit: int,
        reference: str,
        taints: List[str],
        agents: List[str],
    ) -> JobId:
        """Creates a new job object in the db, and schedules daemon tasks."""
        job = "".join(
            random.choice(string.ascii_uppercase + string.digits)
            for _ in range(12)
        )
        obj = Job(
            id=job,
            status="new",
            rule_name=rule_name,
            rule_author=rule_author,
            raw_yara=raw_yara,
            submitted=int(time()),
            files_limit=files_limit,
            reference=reference,
            files_in_progress=0,
            files_processed=0,
            files_matched=0,
            files_errored=0,
            total_files=0,
            agents_left=len(agents),
            datasets_left=0,
            total_datasets=0,
            taints=taints,
        )
        session.add(obj)
        session.commit()

        from . import tasks

        for agent in agents:
            self.__schedule(agent, tasks.start_search, job)
        return job

    def get_job_matches(
        self,
        session: Session,
        job_id: JobId,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> MatchesSchema:
        job = self.get_job(session, job_id)
        if limit is None:
            matches = job.matches[offset:]
        else:
            matches = job.matches[offset : offset + limit]
        return MatchesSchema(job=job, matches=matches)

    def update_job_files(
        self, session: Session, job: JobId, total_files: int
    ) -> int:
        """Add total_files to the specified job, and return a new total."""
        (total_files,) = session.execute(
            update(Job)
            .where(Job.id == job)
            .values(total_files=Job.total_files + total_files)
            .returning(Job.total_files)
        ).one()
        return total_files

    def register_active_agent(
        self,
        session: Session,
        group_id: str,
        ursadb_url: str,
        plugins_spec: Dict[str, Dict[str, str]],
        active_plugins: List[str],
    ) -> None:
        """Update or create a Agent information row in the database.
        Returns the new or existing agent ID."""
        # Currently this is done by workers when starting. In the future,
        # this should be configured by the admin, and workers should just read
        # their configuration from the database.
        entry = session.exec(
            select(AgentGroup).where(AgentGroup.name == group_id)
        ).one_or_none()
        if not entry:
            entry = AgentGroup(name=group_id)
        entry.ursadb_url = ursadb_url
        entry.plugins_spec = plugins_spec
        entry.active_plugins = active_plugins
        session.add(entry)

    def get_active_agents(self, session: Session) -> Dict[str, AgentGroup]:
        agents = session.exec(select(AgentGroup)).all()
        return {agent.name: agent for agent in agents}

    def get_core_config(self) -> Dict[str, str]:
        """Gets a list of configuration fields for the mquery core."""
        return {
            # Autentication-related config
            "auth_enabled": "Enable and force authentication for all users ('true' or 'false')",
            "auth_default_roles": "Comma separated list of roles available to everyone (available roles: admin, user)",
            # OpenID Authentication config
            "openid_url": "OpenID Connect base url",
            "openid_client_id": "OpenID client ID",
            "openid_secret": "Secret used for JWT token verification",
            # Query and performance config
            "query_allow_slow": "Allow users to run queries that will end up scanning the whole malware collection",
        }

    def get_config(self, session: Session) -> List[ConfigSchema]:
        # { plugin_name: { field: description } }
        config_fields: Dict[str, Dict[str, str]] = defaultdict(dict)
        config_fields[MQUERY_PLUGIN_NAME] = self.get_core_config()
        # Merge all config fields
        for agent_spec in self.get_active_agents(session).values():
            for plugin, fields in agent_spec.plugins_spec.items():
                config_fields[plugin].update(fields)
        # Transform fields into ConfigSchema
        # { plugin_name: { field: ConfigSchema } }
        plugin_configs = {
            plugin: {
                key: ConfigSchema(
                    plugin=plugin, key=key, value="", description=description
                )
                for key, description in spec.items()
            }
            for plugin, spec in config_fields.items()
        }
        # Get configuration values for each plugin
        for plugin, spec in plugin_configs.items():
            config = self.get_plugin_config(session, plugin)
            for key, value in config.items():
                if key in plugin_configs[plugin]:
                    plugin_configs[plugin][key].value = value
        # Flatten to the target form
        return [
            plugin_configs[plugin][key]
            for plugin in sorted(plugin_configs.keys())
            for key in sorted(plugin_configs[plugin].keys())
        ]

    def get_plugin_config(
        self, session: Session, plugin_name: str
    ) -> Dict[str, str]:
        entries = session.exec(
            select(ConfigEntry).where(ConfigEntry.plugin == plugin_name)
        ).all()
        return {e.key: e.value for e in entries}

    def get_mquery_config_key(
        self, session: Session, key: str
    ) -> Optional[str]:
        statement = select(ConfigEntry).where(
            and_(
                ConfigEntry.plugin == MQUERY_PLUGIN_NAME,
                ConfigEntry.key == key,
            )
        )
        entry = session.exec(statement).one_or_none()
        return entry.value if entry else None

    def set_config_key(
        self, session: Session, plugin_name: str, key: str, value: str
    ) -> None:
        entry = session.exec(
            select(ConfigEntry).where(
                ConfigEntry.plugin == plugin_name,
                ConfigEntry.key == key,
            )
        ).one_or_none()
        if not entry:
            entry = ConfigEntry(plugin=plugin_name, key=key)
        entry.value = value
        session.add(entry)


def init_db() -> None:
    engine = create_engine(app_config.database.url, echo=True)
    SQLModel.metadata.create_all(engine)


if __name__ == "__main__":
    init_db()

import re
import json
import yaml
from collections import OrderedDict
from hashlib import sha1


from dateutil.relativedelta import relativedelta
from django.db import models
from django.db.models import Q, Count
from django.db.models.query import prefetch_related_objects
from django.contrib.auth.models import Group as UserGroup
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.core.validators import RegexValidator
from django.utils import timezone


from squad.core.utils import random_token, parse_name, join_name, yaml_validator
from squad.core.comparison import TestComparison
from squad.core.statistics import geomean
from squad.core.plugins import Plugin
from squad.core.plugins import PluginListField
from squad.core.plugins import PluginField
from squad.core.plugins import get_plugin_instance


slug_pattern = '[a-zA-Z0-9][a-zA-Z0-9_.-]*'
slug_validator = RegexValidator(regex='^' + slug_pattern)


class GroupManager(models.Manager):

    def accessible_to(self, user):
        projects = Project.objects.accessible_to(user)
        project_ids = [p.id for p in projects]
        group_ids = set([p.group_id for p in projects])
        return self.filter(id__in=group_ids).annotate(project_count=Count('projects', filter=Q(id__in=project_ids)))


class Group(models.Model):
    objects = GroupManager()

    slug = models.CharField(max_length=100, unique=True, validators=[slug_validator], db_index=True)
    name = models.CharField(max_length=100, null=True)
    description = models.TextField(null=True)
    user_groups = models.ManyToManyField(UserGroup)

    def __str__(self):
        return self.slug

    class Meta:
        ordering = ['slug']


class ProjectManager(models.Manager):

    def accessible_to(self, user):
        if user.is_superuser or user.is_staff:
            return self.all()
        else:
            groups = Group.objects.filter(user_groups__in=user.groups.all())
            group_ids = [g['id'] for g in groups.values('id')]
            return self.filter(Q(group_id__in=group_ids) | Q(is_public=True))


class EmailTemplate(models.Model):
    name = models.CharField(max_length=100, unique=True)
    subject = models.CharField(max_length=1024, null=True, blank=True, help_text='Jinja2 template for subject (single line)')
    plain_text = models.TextField(help_text='Jinja2 template for text/plain content')
    html = models.TextField(blank=True, null=True, help_text='Jinja2 template for text/html content')

    def __str__(self):
        return self.name


class Project(models.Model):
    objects = ProjectManager()

    group = models.ForeignKey(Group, related_name='projects')
    slug = models.CharField(max_length=100, validators=[slug_validator], db_index=True)
    name = models.CharField(max_length=100, null=True)
    is_public = models.BooleanField(default=True)
    html_mail = models.BooleanField(default=True)
    moderate_notifications = models.BooleanField(default=False)
    custom_email_template = models.ForeignKey(EmailTemplate, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    important_metadata_keys = models.TextField(null=True, blank=True)
    enabled_plugins_list = PluginListField(
        null=True,
        features=[
            Plugin.postprocess_testrun,
            Plugin.postprocess_testjob,
        ],
    )

    wait_before_notification = models.IntegerField(
        help_text='Wait this many seconds before sending notifications',
        null=True,
        blank=True,
    )
    notification_timeout = models.IntegerField(
        help_text='Force sending build notifications after this many seconds',
        null=True,
        blank=True,
    )

    NOTIFY_ALL_BUILDS = 'all'
    NOTIFY_ON_CHANGE = 'change'
    notification_strategy = models.CharField(
        max_length=32,
        choices=((NOTIFY_ALL_BUILDS, 'All builds'), (NOTIFY_ON_CHANGE, 'Only on change')),
        default='all'
    )

    def __init__(self, *args, **kwargs):
        super(Project, self).__init__(*args, **kwargs)
        self.__status__ = None

    @property
    def status(self):
        if not self.__status__:
            self.__status__ = ProjectStatus.objects.filter(
                build__project=self
            ).latest('created_at')
        return self.__status__

    def accessible_to(self, user):
        return self.is_public or self.writable_by(user)

    def writable_by(self, user):
        return user.is_superuser or user.is_staff or self.group.user_groups.filter(id__in=user.groups.all()).exists()

    @property
    def full_name(self):
        return str(self.group) + '/' + self.slug

    def __str__(self):
        return self.full_name

    class Meta:
        unique_together = ('group', 'slug',)
        ordering = ['group', 'slug']

    @property
    def enabled_plugins(self):
        return self.enabled_plugins_list


class Token(models.Model):
    project = models.ForeignKey(Project, related_name='tokens', null=True)
    key = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=100)

    def save(self, **kwargs):
        if not self.key:
            self.key = random_token(64)
        super(Token, self).save(**kwargs)

    def __str__(self):
        return self.description


class PatchSource(models.Model):
    """
    A patch is source is a platform from where a patch comes from, e.g. github,
    a gitlab instance, a gerrit instance. The *implementation* field specifies
    which plugin should handle the implementation details for that given patch
    source.
    """
    name = models.CharField(max_length=256, unique=True)
    username = models.CharField(max_length=128)
    url = models.URLField()
    token = models.CharField(max_length=1024)
    implementation = PluginField(
        default='null',
        features=[
            Plugin.notify_patch_build_created,
            Plugin.notify_patch_build_finished,
        ],
    )

    def get_implementation(self):
        return get_plugin_instance(self.implementation)


class Build(models.Model):
    project = models.ForeignKey(Project, related_name='builds')
    version = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    datetime = models.DateTimeField()

    patch_source = models.ForeignKey(PatchSource, null=True, blank=True)
    patch_baseline = models.ForeignKey('Build', null=True, blank=True)
    patch_id = models.CharField(max_length=1024, null=True, blank=True)

    class Meta:
        unique_together = ('project', 'version',)
        ordering = ['datetime']

    def save(self, *args, **kwargs):
        if not self.datetime:
            self.datetime = timezone.now()
        super(Build, self).save(*args, **kwargs)

    def __str__(self):
        return '%s (%s)' % (self.version, self.datetime)

    @staticmethod
    def prefetch_related(builds):
        prefetch_related_objects(
            builds,
            'project',
            'project__group',
            'test_runs',
            'test_runs__environment',
            'test_runs__tests',
            'test_runs__tests__suite',
        )

    @property
    def test_summary(self):
        return TestSummary(self)

    __metadata__ = None

    @property
    def metadata(self):
        """
        The build metadata is the union of the metadata in its test runs.
        Common keys with different values are transformed into a list with each
        of the different values.
        """
        if self.__metadata__ is None:
            metadata = {}
            for test_run in self.test_runs.defer(None).all():
                for key, value in test_run.metadata.items():
                    metadata.setdefault(key, [])
                    if value not in metadata[key]:
                        metadata[key].append(value)
            for key in metadata.keys():
                if len(metadata[key]) == 1:
                    metadata[key] = metadata[key][0]
                else:
                    metadata[key] = sorted(metadata[key], key=str)
            self.__metadata__ = metadata
        return self.__metadata__

    @property
    def important_metadata(self):
        wanted = (self.project.important_metadata_keys or '').splitlines()
        m = self.metadata
        if len(wanted):
            return {k: m[k] for k in wanted if k in m}
        else:
            return self.metadata

    @property
    def has_extra_metadata(self):
        if set(self.important_metadata.keys()) == set(self.metadata.keys()):
            return False
        return True

    @property
    def finished(self):
        """
        A finished build is a build that satisfies one of the following conditions:

        * it has no pending CI test jobs.
        * it has no submitted CI test jobs, and has at least N test runs for each of
          the project environments, where N is configured in
          Environment.expected_test_runs. Environment.expected_test_runs is
          interpreted as follows:

            * None (empty):  there must be at least one test run for that
              environment.
            * 0: the environment is ignored, i.e. any amount of test runs will
              be ok, including 0.
            * N > 0: at least N test runs are expected for that environment

        """
        reasons = []

        # XXX note that by using test_jobs here, we are adding an implicit
        # dependency on squad.ci, what in theory violates our architecture.
        testjobs = self.test_jobs
        if testjobs.count() > 0:
            if testjobs.filter(fetched=False).count() > 0:
                # a build that has pending CI jobs is NOT finished
                reasons.append("There are unfinished CI jobs")
            else:
                # carry on, and check whether the number of expected test runs
                # per environment is satisfied.
                pass

        # builds with no CI jobs are finished when each environment has
        # received the expected amount of test runs
        testruns = {
            e.id: {
                'name': str(e),
                'expected': e.expected_test_runs,
                'received': 0
            }
            for e in self.project.environments.all()
        }

        for t in self.test_runs.filter(completed=True).all():
            testruns[t.environment_id]['received'] += 1

        for env, count in testruns.items():
            expected = count['expected']
            received = count['received']
            env_name = count['name']
            if expected == 0:
                continue
            if received == 0:
                reasons.append("No test runs for %s received so far" % env_name)
            if expected and received < expected:
                reasons.append(
                    "%d test runs expected for %s, but only %d received so far" % (
                        expected,
                        env_name,
                        received,
                    )
                )
        return (len(reasons) == 0, reasons)

    @property
    def test_suites_by_environment(self):
        test_runs = self.test_runs.prefetch_related(
            'tests',
            'tests__suite',
            'environment',
        )
        result = OrderedDict()
        envlist = set([t.environment for t in test_runs])
        for env in sorted(envlist, key=lambda env: env.slug):
            result[env] = dict()
        for tr in test_runs:
            for t in tr.tests.all():
                if t.suite in result[tr.environment].keys():
                    result[tr.environment][t.suite][t.status] += 1
                else:
                    result[tr.environment].setdefault(t.suite, {'fail': 0, 'pass': 0, 'skip': 0, 'xfail': 0})
                    result[tr.environment][t.suite][t.status] += 1
        for env in result.keys():
            # there should only be one key in the most nested dict
            result[env] = sorted(
                result[env].items(),
                key=lambda suite_dict: suite_dict[0].slug)

        return result


class Environment(models.Model):
    project = models.ForeignKey(Project, related_name='environments')
    slug = models.CharField(max_length=100, validators=[slug_validator], db_index=True)
    name = models.CharField(max_length=100, null=True, blank=True)
    expected_test_runs = models.IntegerField(default=None, null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    class Meta:
        unique_together = ('project', 'slug',)

    def __str__(self):
        return self.name or self.slug


class TestRunManager(models.Manager):

    use_for_related_fields = True

    def get_queryset(self, *args, **kwargs):
        return super(TestRunManager, self).get_queryset(*args, **kwargs).defer(
            "tests_file",
            "metrics_file",
            "log_file",
            "metadata_file",
        )


class TestRun(models.Model):
    build = models.ForeignKey(Build, related_name='test_runs')
    environment = models.ForeignKey(Environment, related_name='test_runs')
    created_at = models.DateTimeField(auto_now_add=True)

    # these fields are potentially very large
    tests_file = models.TextField(null=True)
    metrics_file = models.TextField(null=True)
    log_file = models.TextField(null=True)
    metadata_file = models.TextField(null=True)

    # custom manager to skip potentially large fields by default
    objects = TestRunManager()

    completed = models.BooleanField(default=True)

    # fields that should be provided in a submitted metadata JSON
    datetime = models.DateTimeField(null=False)
    build_url = models.CharField(null=True, max_length=2048)
    job_id = models.CharField(null=True, max_length=128)
    job_status = models.CharField(null=True, max_length=128)
    job_url = models.CharField(null=True, max_length=2048)
    resubmit_url = models.CharField(null=True, max_length=2048)

    data_processed = models.BooleanField(default=False)
    status_recorded = models.BooleanField(default=False)

    class Meta:
        unique_together = ('build', 'job_id')

    def save(self, *args, **kwargs):
        if not self.datetime:
            self.datetime = timezone.now()
        if self.__metadata__:
            self.metadata_file = json.dumps(self.__metadata__)
        super(TestRun, self).save(*args, **kwargs)

    @property
    def project(self):
        return self.build.project

    __metadata__ = None

    @property
    def metadata(self):
        if self.__metadata__ is None:
            if self.metadata_file:
                self.__metadata__ = json.loads(self.metadata_file)
            else:
                self.__metadata__ = {}
        return self.__metadata__

    def __str__(self):
        return self.job_id and ('#%s' % self.job_id) or ('(%s)' % self.id)


class Attachment(models.Model):
    test_run = models.ForeignKey(TestRun, related_name='attachments')
    filename = models.CharField(null=False, max_length=1024)
    data = models.BinaryField(default=None)
    length = models.IntegerField(default=None)


class SuiteMetadata(models.Model):
    suite = models.CharField(max_length=256, db_index=True)
    kind = models.CharField(
        max_length=6,
        choices=(
            ('suite', 'Suite'),
            ('test', 'Test'),
            ('metric', 'Metric'),
        ),
        db_index=True,
    )
    name = models.CharField(max_length=256, null=True, db_index=True)
    description = models.TextField(null=True, blank=True)
    instructions_to_reproduce = models.TextField(null=True, blank=True)

    class Meta:
        unique_together = ('kind', 'suite', 'name')
        verbose_name_plural = 'Suite metadata'

    def __str__(self):
        if self.name == '-':
            return self.suite
        else:
            return join_name(self.suite, self.name)


class Suite(models.Model):
    project = models.ForeignKey(Project, related_name='suites')
    slug = models.CharField(max_length=256, validators=[slug_validator], db_index=True)
    name = models.CharField(max_length=256, null=True, blank=True)
    metadata = models.ForeignKey(
        SuiteMetadata,
        null=True,
        related_name='+',
        limit_choices_to={'kind': 'suite'},
    )

    class Meta:
        unique_together = ('project', 'slug',)

    def __str__(self):
        return self.name or self.slug


class SuiteVersion(models.Model):
    suite = models.ForeignKey(Suite, related_name='versions')
    version = models.CharField(max_length=40, null=True)
    first_seen = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('suite', 'version')

    def __str__(self):
        return '%s %s' % (self.suite.name, self.version)


class Test(models.Model):
    test_run = models.ForeignKey(TestRun, related_name='tests')
    suite = models.ForeignKey(Suite)
    metadata = models.ForeignKey(
        SuiteMetadata,
        null=True,
        related_name='+',
        limit_choices_to={'kind': 'test'},
    )
    name = models.CharField(max_length=256, db_index=True)
    result = models.NullBooleanField()
    log = models.TextField(null=True, blank=True)
    known_issues = models.ManyToManyField('KnownIssue')
    has_known_issues = models.NullBooleanField()

    def __str__(self):
        return self.name

    @property
    def status(self):
        if self.result:
            return 'pass'
        elif self.result is None:
            return 'skip'
        else:
            if self.has_known_issues:
                return 'xfail'
            else:
                return 'fail'

    @property
    def full_name(self):
        return join_name(self.suite.slug, self.name)

    @staticmethod
    def prefetch_related(tests):
        prefetch_related_objects(
            tests,
            'known_issues',
            'test_run',
            'test_run__environment',
            'test_run__build',
            'test_run__build__project',
            'test_run__build__project__group',
        )

    class History(object):
        def __init__(self, since, count, last_different):
            self.since = since
            self.count = count
            self.last_different = last_different

    __history__ = None

    @property
    def history(self):
        if self.__history__:
            return self.__history__

        date = self.test_run.build.datetime
        previous_tests = Test.objects.filter(
            suite=self.suite,
            name=self.name,
            test_run__build__datetime__lt=date,
            test_run__environment=self.test_run.environment,
        ).exclude(id=self.id).order_by("-test_run__build__datetime")
        since = None
        count = 0
        last_different = None
        for test in list(previous_tests):
            if test.result == self.result:
                since = test
                count += 1
            else:
                last_different = test
                break
        self.__history__ = Test.History(since, count, last_different)
        return self.__history__

    class Meta:
        ordering = ['name']


class MetricManager(models.Manager):

    def by_full_name(self, name):
        (suite, metric) = parse_name(name)
        return self.filter(suite__slug=suite, name=metric)


class Metric(models.Model):
    test_run = models.ForeignKey(TestRun, related_name='metrics')
    suite = models.ForeignKey(Suite)
    metadata = models.ForeignKey(
        SuiteMetadata,
        null=True,
        related_name='+',
        limit_choices_to={'kind': 'metric'},
    )
    name = models.CharField(max_length=100)
    result = models.FloatField()
    measurements = models.TextField()  # comma-separated float numbers

    objects = MetricManager()

    @property
    def measurement_list(self):
        if self.measurements:
            return [float(n) for n in self.measurements.split(',')]
        else:
            return []

    @property
    def full_name(self):
        return join_name(self.suite.slug, self.name)

    def __str__(self):
        return '%s: %f' % (self.name, self.result)


class StatusManager(models.Manager):

    def by_suite(self):
        return self.exclude(suite=None)

    def overall(self):
        return self.filter(suite=None)


class TestSummaryBase(object):

    @property
    def tests_total(self):
        return self.tests_pass + self.tests_fail + self.tests_xfail + self.tests_skip

    def __percent__(self, ntests):
        if ntests > 0:
            return 100 * (float(ntests) / float(self.tests_total))
        else:
            return 0

    @property
    def pass_percentage(self):
        return self.__percent__(self.tests_pass)

    @property
    def fail_percentage(self):
        return self.__percent__(self.tests_fail + self.tests_xfail)

    @property
    def skip_percentage(self):
        return self.__percent__(self.tests_skip)

    @property
    def has_tests(self):
        return self.tests_total > 0


class Status(models.Model, TestSummaryBase):
    test_run = models.ForeignKey(TestRun, related_name='status')
    suite = models.ForeignKey(Suite, null=True)
    suite_version = models.ForeignKey(SuiteVersion, null=True)

    tests_pass = models.IntegerField(default=0)
    tests_fail = models.IntegerField(default=0)
    tests_xfail = models.IntegerField(default=0)
    tests_skip = models.IntegerField(default=0)
    metrics_summary = models.FloatField(default=0.0)
    has_metrics = models.BooleanField(default=False)

    objects = StatusManager()

    class Meta:
        unique_together = ('test_run', 'suite',)

    @property
    def environment(self):
        return self.test_run.environment

    @property
    def tests(self):
        return self.test_run.tests.filter(suite=self.suite)

    @property
    def metrics(self):
        return self.test_run.metrics.filter(suite=self.suite)

    def __str__(self):
        if self.suite:
            name = self.suite.slug + ' on ' + self.environment.slug
        else:
            name = self.environment.slug
        return '%s: %f, %d%% pass' % (name, self.metrics_summary, self.pass_percentage)


class ProjectStatus(models.Model, TestSummaryBase):
    """
    Represents a "checkpoint" of a project status in time. It is used by the
    notification system to know what was the project status at the time of the
    last notification.
    """
    build = models.OneToOneField('Build', related_name='status')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(null=True)
    finished = models.BooleanField(default=False)
    notified = models.BooleanField(default=False)
    notified_on_timeout = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)

    metrics_summary = models.FloatField()
    has_metrics = models.BooleanField(default=False)

    tests_pass = models.IntegerField(default=0)
    tests_fail = models.IntegerField(default=0)
    tests_xfail = models.IntegerField(default=0)
    tests_skip = models.IntegerField(default=0)

    test_runs_total = models.IntegerField(default=0)
    test_runs_completed = models.IntegerField(default=0)
    test_runs_incomplete = models.IntegerField(default=0)

    regressions = models.TextField(
        null=True,
        blank=True,
        validators=[yaml_validator]
    )
    fixes = models.TextField(
        null=True,
        blank=True,
        validators=[yaml_validator]
    )

    class Meta:
        verbose_name_plural = "Project statuses"

    @classmethod
    def create_or_update(cls, build):
        """
        Creates (or updates) a new ProjectStatus for the given build and
        returns it.
        """

        test_summary = build.test_summary
        metrics_summary = MetricsSummary(build)
        now = timezone.now()
        test_runs_total = build.test_runs.count()
        test_runs_completed = build.test_runs.filter(completed=True).count()
        test_runs_incomplete = build.test_runs.filter(completed=False).count()
        regressions = None
        fixes = None

        previous_build = Build.objects.filter(
            status__finished=True,
            datetime__lt=build.datetime,
            project=build.project,
        ).order_by('build__datetime').last()
        if previous_build is not None:
            comparison = TestComparison(previous_build, build)
            if comparison.regressions:
                regressions = yaml.dump(comparison.regressions)
            if comparison.fixes:
                fixes = yaml.dump(comparison.fixes)

        finished, _ = build.finished
        data = {
            'tests_pass': test_summary.tests_pass,
            'tests_fail': test_summary.tests_fail,
            'tests_xfail': test_summary.tests_xfail,
            'tests_skip': test_summary.tests_skip,
            'metrics_summary': metrics_summary.value,
            'has_metrics': metrics_summary.has_metrics,
            'last_updated': now,
            'finished': finished,
            'test_runs_total': test_runs_total,
            'test_runs_completed': test_runs_completed,
            'test_runs_incomplete': test_runs_incomplete,
            'regressions': regressions,
            'fixes': fixes
        }

        status, created = cls.objects.get_or_create(build=build, defaults=data)
        if not created and test_summary.tests_total >= status.tests_total:
            # XXX the test above for the new total number of tests prevents
            # results that arrived earlier, but are only being processed now,
            # from overwriting a ProjectStatus created by results that arrived
            # later but were already processed.
            status.tests_pass = test_summary.tests_pass
            status.tests_fail = test_summary.tests_fail
            status.tests_xfail = test_summary.tests_xfail
            status.tests_skip = test_summary.tests_skip
            status.metrics_summary = metrics_summary.value
            status.has_metrics = metrics_summary.has_metrics
            status.last_updated = now
            finished, _ = build.finished
            status.finished = finished
            status.build = build
            status.test_runs_total = test_runs_total
            status.test_runs_completed = test_runs_completed
            status.test_runs_incomplete = test_runs_incomplete
            status.regressions = regressions
            status.fixes = fixes
            status.save()
        return status

    def __str__(self):
        return "%s, build %s" % (self.build.project, self.build.version)

    def get_previous(self):
        return ProjectStatus.objects.filter(
            finished=True,
            build__datetime__lt=self.build.datetime,
            build__project=self.build.project,
        ).order_by('build__datetime').last()

    def __get_yaml_field__(self, field_value):
        if field_value is not None:
            return yaml.load(field_value)
        return {}

    def get_regressions(self):
        return self.__get_yaml_field__(self.regressions)

    def get_fixes(self):
        return self.__get_yaml_field__(self.fixes)


class NotificationDelivery(models.Model):

    status = models.ForeignKey('ProjectStatus', related_name='deliveries')
    subject = models.CharField(max_length=40, null=True, blank=True)
    txt = models.CharField(max_length=40, null=True, blank=True)
    html = models.CharField(max_length=40, null=True, blank=True)

    class Meta:
        unique_together = ('status', 'subject', 'txt', 'html')

    @classmethod
    def exists(cls, status, subject, txt, html):
        subject_hash = sha1(subject.encode()).hexdigest()
        txt_hash = sha1(txt.encode()).hexdigest()
        html_hash = sha1(html.encode()).hexdigest()
        obj, created = cls.objects.get_or_create(
            status=status,
            subject=subject_hash,
            txt=txt_hash,
            html=html_hash,
        )
        return (not created)


class InvalidTestStatus(Exception):

    def __init__(self, status):
        super(InvalidTestStatus, self).__init__('Invalid status: %s' % status)


class TestSummary(TestSummaryBase):
    def __init__(self, build):
        self.tests_pass = 0
        self.tests_fail = 0
        self.tests_xfail = 0
        self.tests_skip = 0
        self.failures = OrderedDict()

        tests = OrderedDict()
        test_runs = build.test_runs.prefetch_related(
            'environment',
            'tests',
            'tests__suite'
        ).order_by('id')

        for run in test_runs.all():
            for test in run.tests.all():
                tests[(run.environment, test.suite, test.name)] = test

        for context, test in tests.items():
            if test.status == 'pass':
                self.tests_pass += 1
            elif test.status == 'fail':
                self.tests_fail += 1
            elif test.status == 'xfail':
                self.tests_xfail += 1
            elif test.status == 'skip':
                self.tests_skip += 1
            else:
                raise InvalidTestStatus(test.status)
            if test.status == 'fail':
                env = test.test_run.environment.slug
                if env not in self.failures:
                    self.failures[env] = []
                self.failures[env].append(test)


class MetricsSummary(object):

    def __init__(self, build):
        metrics = Metric.objects.filter(test_run__build_id=build.id).all()
        values = [m.result for m in metrics]
        self.value = geomean(values)
        self.has_metrics = len(values) > 0


class Subscription(models.Model):
    project = models.ForeignKey(Project, related_name='subscriptions')
    email = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        validators=[EmailValidator()]
    )
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        default=None
    )

    class Meta:
        unique_together = ('project', 'user',)

    def _validate_email(self):
        if (not self.email) == (not self.user):
            raise ValidationError("Subscription object must have exactly one of 'user' and 'email' fields populated.")

    def save(self, *args, **kwargs):
        self._validate_email()
        super().save(*args, **kwargs)

    def clean(self):
        self._validate_email()

    def get_email(self):
        if self.user and self.user.email:
            return self.user.email
        return self.email

    def __str__(self):
        return '%s on %s' % (self.get_email(), self.project)


class AdminSubscription(models.Model):
    project = models.ForeignKey(Project, related_name='admin_subscriptions')
    email = models.CharField(max_length=1024, validators=[EmailValidator()])

    def __str__(self):
        return '%s on %s' % (self.email, self.project)


class KnownIssue(models.Model):
    title = models.CharField(max_length=1024)
    test_name = models.CharField(max_length=1024)

    url = models.URLField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    active = models.BooleanField(default=True)
    intermittent = models.BooleanField(default=False)
    environments = models.ManyToManyField(Environment)

    @classmethod
    def active_by_environment(cls, environment):
        return cls.objects.filter(active=True, environments=environment)

    @classmethod
    def active_by_project_and_test(cls, project, test_name=None):
        qs = cls.objects.filter(active=True, environments__project=project).prefetch_related('environments')
        if test_name:
            qs = qs.filter(test_name=test_name)
        return qs.distinct()

"""
Main class of Robocop module. Gather files for scan, checkers and parse cli arguments and scan files.
"""
import inspect
import sys
import os
from pathlib import Path

from robot.api import get_resource_model

import robocop.exceptions
from robocop import checkers
from robocop import reports
from robocop.config import Config
from robocop.utils import DisablersFinder, FileType, FileTypeChecker


class Robocop:
    """
    Main class for running the checks.

    If you want to run checks with non default configuration create your own ``Config`` and pass it to ``Robocop``.
    Use ``Robocop.run()`` method to start analysis. If ``from_cli`` is set to ``False`` it will return
    list of found issues in JSON format.

    Example::

        import robocop
        from robocop.config import Config

        config = Config()
        config.include = {'1003'}
        config.paths = ['tests\\atest\\rules\\section-out-of-order']

        robocop_runner = robocop.Robocop(config=config)
        issues = robocop_runner.run()

    """
    def __init__(self, from_cli=False, config=None):
        self.files = {}
        self.checkers = []
        self.rules = {}
        self.reports = dict()
        self.disabler = None
        self.root = os.getcwd()
        self.config = Config() if config is None else config
        self.from_cli = from_cli
        if from_cli:
            self.config.parse_opts()
        else:
            self.config.reports.add('json_report')
        self.out = self.set_output()

    def set_output(self):
        """ Set output for printing to file if configured. Else use standard output """
        return self.config.output or None

    def write_line(self, line):
        """ Print line using file=self.out parameter (set in `set_output` method) """
        print(line, file=self.out)

    def reload_config(self):
        """ Reload checkers and reports based on current config """
        self.load_checkers()
        self.list_checkers()
        self.load_reports()
        self.configure_checkers_or_reports()

    def run(self):
        """ Entry point for running scans """
        self.reload_config()

        self.recognize_file_types()
        self.run_checks()
        self.make_reports()
        if self.config.output and not self.out.closed:
            self.out.close()
        if self.from_cli:
            sys.exit(self.reports['return_status'].return_status)
        else:
            return self.reports['json_report'].issues

    def recognize_file_types(self):
        """
        Pre-parse files to recognize their types. If the filename is `__init__.*`, the type is `INIT`.
        Files with .resource extension are `RESOURCE` type.
        If the file is imported somewhere then file type is `RESOURCE`. Otherwise file type is `GENERAL`.
        These types are important since they are used to define parsing class for robot API.
        """
        files = self.config.paths
        file_type_checker = FileTypeChecker(self.config.exec_dir)
        for file in self.get_files(files, self.config.recursive):
            if '__init__' in file.name:
                file_type = FileType.INIT
            elif file.suffix.lower() == '.resource':
                file_type = FileType.RESOURCE
            else:
                file_type = FileType.GENERAL
            file_type_checker.source = file
            model = file_type.get_parser()(str(file))
            file_type_checker.visit(model)
            self.files[file] = (file_type, model)

        for resource in file_type_checker.resource_files:
            if resource in self.files and self.files[resource][0].value != FileType.RESOURCE:
                self.files[resource] = (FileType.RESOURCE, get_resource_model(str(resource)))

    def run_checks(self):
        files_with_issues = 0
        for file in self.files:
            found_issues = []
            self.register_disablers(file)
            if self.disabler.file_disabled:
                continue
            model = self.files[file][1]
            for checker in self.checkers:
                if checker.disabled:
                    continue
                checker.source = str(file)
                checker.scan_file(model)
                found_issues += checker.issues
                checker.issues.clear()
            if found_issues:
                files_with_issues += 1
            found_issues.sort()
            for issue in found_issues:
                self.report(issue)
        if 'file_stats' in self.reports:
            self.reports['file_stats'].files_count = len(self.files)
            self.reports['file_stats'].files_with_issues = files_with_issues

    def register_disablers(self, file):
        """ Parse content of file to find any disabler statements like # robocop: disable=rulename """
        self.disabler = DisablersFinder(file, self)

    def report(self, rule_msg):
        if not rule_msg.enabled:  # disabled from cli
            return
        if self.disabler.is_rule_disabled(rule_msg):  # disabled from source code
            return
        for report in self.reports.values():
            report.add_message(rule_msg)
        try:
            source_rel = os.path.relpath(os.path.expanduser(rule_msg.source), self.root)
        except ValueError:
            source_rel = rule_msg.source
        self.log_message(source=rule_msg.source,
                         source_rel=source_rel,
                         line=rule_msg.line,
                         col=rule_msg.col,
                         severity=rule_msg.severity.value,
                         rule_id=rule_msg.rule_id,
                         desc=rule_msg.desc,
                         msg_name=rule_msg.name)

    def log_message(self, **kwargs):
        self.write_line(self.config.format.format(**kwargs))

    def load_checkers(self):
        self.checkers = []
        self.rules = {}
        checkers.init(self)

    def list_checkers(self):
        if not (self.config.list or self.config.list_configurables):
            return
        rule_by_id = {msg.rule_id: msg for checker in self.checkers for msg in checker.rules_map.values()}
        rule_ids = sorted([key for key in rule_by_id])
        for rule_id in rule_ids:
            if self.config.list:
                if not rule_by_id[rule_id].matches_pattern(self.config.list):
                    continue
                print(rule_by_id[rule_id])
            else:
                if not rule_by_id[rule_id].matches_pattern(self.config.list_configurables):
                    continue
                print(f"{rule_by_id[rule_id]}\n    {rule_by_id[rule_id].available_configurables()}")
        sys.exit()

    def load_reports(self):
        self.reports = dict()
        classes = inspect.getmembers(reports, inspect.isclass)
        available_reports = 'Available reports:\n'
        for report_class in classes:
            if not issubclass(report_class[1], reports.Report):
                continue
            report = report_class[1]()
            if not hasattr(report, 'name'):
                continue
            if 'all' in self.config.reports or report.name in self.config.reports:
                self.reports[report.name] = report
            available_reports += f'{report.name} - {report.description}\n'
        if self.config.list_reports:
            available_reports += 'all - Turns on all available reports'
            print(available_reports)
            sys.exit()

    def register_checker(self, checker):
        if not self.any_rule_enabled(checker):
            checker.disabled = True
        for rule_name, rule in checker.rules_map.items():
            if rule_name in self.rules:
                (_, checker_prev) = self.rules[rule_name]
                raise robocop.exceptions.DuplicatedRuleError('name', rule_name, checker, checker_prev)
            if rule.rule_id in self.rules:
                (_, checker_prev) = self.rules[rule.rule_id]
                raise robocop.exceptions.DuplicatedRuleError('id', rule.rule_id, checker, checker_prev)
            self.rules[rule_name] = (rule, checker)
            self.rules[rule.rule_id] = (rule, checker)
        self.checkers.append(checker)

    def make_reports(self):
        for report in self.reports.values():
            output = report.get_report()
            if output is not None:
                self.write_line(output)

    def get_files(self, files_or_dirs, recursive):
        for file in files_or_dirs:
            yield from self.get_absolute_path(Path(file), recursive)

    def get_absolute_path(self, path, recursive):
        if not path.exists():
            raise robocop.exceptions.FileError(path)
        if self.config.is_path_ignored(path):
            return
        if path.is_file():
            if self.should_parse(path):
                yield path.absolute()
        elif path.is_dir():
            for file in path.iterdir():
                if file.is_dir() and not recursive:
                    continue
                yield from self.get_absolute_path(file, recursive)

    def should_parse(self, file):
        """ Check if file extension is in list of supported file types (can be configured from cli) """
        return file.suffix and file.suffix.lower() in self.config.filetypes

    def any_rule_enabled(self, checker):
        for name, rule in checker.rules_map.items():
            rule.enabled = self.config.is_rule_enabled(rule)
            checker.rules_map[name] = rule
        return any(msg.enabled for msg in checker.rules_map.values())

    def configure_checkers_or_reports(self):
        for config in self.config.configure:
            if config.count(':') < 2:
                raise robocop.exceptions.ConfigGeneralError(
                    f"Provided invalid config: '{config}' (general pattern: <rule>:<param>:<value>)")
            rule_or_report, param, value, *values = config.split(':')
            if rule_or_report in self.rules:
                msg, checker = self.rules[rule_or_report]
                if param == 'severity':
                    self.rules[rule_or_report] = (msg.change_severity(value), checker)
                else:
                    configurable = msg.get_configurable(param)
                    if configurable is None:
                        available_conf = msg.available_configurables()
                        raise robocop.exceptions.ConfigGeneralError(
                            f"Provided param '{param}' for rule '{rule_or_report}' does not exist. {available_conf}")
                    checker.configure(configurable[1], configurable[2](value))
            elif rule_or_report in self.reports:
                self.reports[rule_or_report].configure(param, value, *values)
            else:
                raise robocop.exceptions.ConfigGeneralError(
                    f"Provided rule or report '{rule_or_report}' does not exist")


def run_robocop():
    linter = Robocop(from_cli=True)
    linter.run()

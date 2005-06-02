#!/usr/bin/env python

import optparse
import fnmatch
import os
import sys
from cStringIO import StringIO
import re
import textwrap
from paste.util.thirdparty import load_new_module
string = load_new_module('string', (2, 4))

from paste import pyconfig
from paste import urlparser
from paste import server
from paste import CONFIG
from paste.util import plugin
from paste.util import import_string

class InvalidCommand(Exception):
    pass

def find_template_info(args):
    """
    Given command-line arguments, this finds the app template
    (paste.app_templates.<template_name>.command).  It looks for a
    -t or --template option (but ignores all other options), and if
    none then looks in server.conf for a template_name option.

    Returns server_conf_fn, template_name, template_dir, module
    """
    template_name = None
    template_name, rest = find_template_option(args)
    server_conf_fn = None
    if template_name:
        next_template_name, rest = find_template_option(rest)
        if next_template_name:
            raise InvalidCommand(
                'You cannot give two templates on the commandline '
                '(first I found %r, then %r)'
                % (template_name, next_template_name))
    else:
        server_conf_fn, template_name = find_template_config(args)
    if not template_name:
        template_name = 'default'
    template_mod = plugin.load_plugin_module(
        'app_templates', 'paste.app_templates',
        template_name, '_tmpl')
    return (server_conf_fn, template_name,
            os.path.dirname(template_mod.__file__), template_mod)

def find_template_option(args):
    copy = args[:]
    while copy:
        if copy[0] == '--':
            return None, copy
        if copy[0] == '-t' or copy[0] == '--template':
            if not copy[1:] or copy[1].startswith('-'):
                raise InvalidCommand(
                    '%s needs to be followed with a template name' % copy[0])
            return copy[1], copy[2:]
        if copy[0].startswith('-t'):
            return copy[0][2:], copy[1:]
        if copy[0].startswith('--template='):
            return copy[0][len('--template='):], copy[1:]
        copy.pop(0)
    return None, []

def find_template_config(args):
    conf_fn = os.path.join(os.getcwd(), 'server.conf')
    if not os.path.exists(conf_fn):
        return None, None
    conf = pyconfig.Config(with_default=True)
    conf.load(conf_fn)
    return conf_fn, conf.get('app_template')

def run(args):
    try:
        server_conf_fn, name, dir, mod = find_template_info(args)
    except InvalidCommand, e:
        print str(e)
        return 2
    return mod.run(args, name, dir, mod, server_conf_fn)

class CommandRunner(object):

    def __init__(self):
        self.commands = {}
        self.command_aliases = {}
        self.register_standard_commands()
        self.server_conf_fn = None

    def run(self, argv, template_name, template_dir, template_module,
            server_conf_fn):
        self.server_conf_fn = server_conf_fn
        invoked_as = argv[0]
        args = argv[1:]
        for i in range(len(args)):
            if not args[i].startswith('-'):
                # this must be a command
                command = args[i].lower()
                del args[i]
                break
        else:
            # no command found
            self.invalid('No COMMAND given (try "%s help")'
                         % os.path.basename(invoked_as))
        real_command = self.command_aliases.get(command, command)
        if real_command not in self.commands.keys():
            self.invalid('COMMAND %s unknown' % command)
        runner = self.commands[real_command](
            invoked_as, command, args, self,
            template_name, template_dir, template_module)
        runner.run()

    def register(self, command):
        name = command.name
        self.commands[name] = command
        for alias in command.aliases:
            self.command_aliases[alias] = name

    def invalid(self, msg, code=2):
        print msg
        sys.exit(code)

    def register_standard_commands(self):
        self.register(CommandHelp)
        self.register(CommandList)
        self.register(CommandServe)
        self.register(CommandInstall)

############################################################
## Command framework
############################################################

def standard_parser(verbose=True, simulate=True, interactive=False):
    parser = optparse.OptionParser()
    if verbose:
        parser.add_option('-v', '--verbose',
                          help='Be verbose (multiple times for more verbosity)',
                          action='count',
                          dest='verbose',
                          default=0)
    if simulate:
        parser.add_option('-n', '--simulate',
                          help="Don't actually do anything (implies -v)",
                          action='store_true',
                          dest='simulate')
    if interactive:
        parser.add_option('-i', '--interactive',
                          help="Ask before doing anything (use twice to be more careful)",
                          action="count",
                          dest="interactive",
                          default=0)
    parser.add_option('-t', '--template',
                      help='Use this template',
                      metavar='NAME',
                      dest='template_name')
    return parser

class Command(object):

    min_args = 0
    min_args_error = 'You must provide at least %(min_args)s arguments'
    max_args = 0
    max_args_error = 'You must provide no more than %(max_args)s arguments'
    aliases = ()
    required_args = []
    description = None
    
    def __init__(self, invoked_as, command_name, args, runner,
                 template_name, template_dir, template_module):
        self.invoked_as = invoked_as
        self.command_name = command_name
        self.raw_args = args
        self.runner = runner
        self.template_name = template_name
        self.template_dir = template_dir
        self.template_module = template_module

    def run(self):
        self.parse_args(self.raw_args)
        if (getattr(self.options, 'simulate', False)
            and not self.options.verbose):
            self.options.verbose = 1
        if self.min_args is not None and len(self.args) < self.min_args:
            self.runner.invalid(
                self.min_args_error % {'min_args': self.min_args,
                                       'actual_args': len(self.args)})
        if self.max_args is not None and len(self.args) > self.max_args:
            self.runner.invalid(
                self.max_args_error % {'max_args': self.max_args,
                                       'actual_args': len(self.args)})
        for var_name, option_name in self.required_args:
            if not getattr(self.options, var_name, None):
                self.runner.invalid(
                    'You must provide the option %s' % option_name)
        self.command()

    def parse_args(self, args):
        self.parser.usage = "%%prog [options]\n%s" % self.summary
        self.parser.prog = '%s %s' % (
            os.path.basename(self.invoked_as),
            self.command_name)
        if self.description:
            self.parser.description = self.description
        self.options, self.args = self.parser.parse_args(args)

    def ask(self, prompt, safe=False, default=True):
        if self.options.interactive >= 2:
            default = safe
        if default:
            prompt += ' [Y/n]? '
        else:
            prompt += ' [y/N]? '
        while 1:
            response = raw_input(prompt).strip()
            if not response.strip():
                return default
            if response and response[0].lower() in ('y', 'n'):
                return response[0].lower() == 'y'
            print 'Y or N please'

    def _get_prog_name(self):
        return os.path.basename(self.invoked_as)
    prog_name = property(_get_prog_name)

############################################################
## Standard commands
############################################################
    
class CommandList(Command):

    name = 'list'
    summary = 'Show available templates'

    parser = standard_parser(simulate=False)

    max_args = 1

    def command(self):
        any = False
        app_template_dir = os.path.join(os.path.dirname(__file__), 'app_templates')
        for name in os.listdir(app_template_dir):
            dir = os.path.join(app_template_dir, name)
            if not os.path.exists(os.path.join(dir, 'description.txt')):
                if self.options.verbose >= 2:
                    print 'Skipping %s (no description.txt)' % dir
                continue
            if self.args and not fnmatch.fnmatch(name, self.args[0]):
                continue
            if not self.options.verbose:
                print '%s: %s\n' % (
                    name, self.template_description().splitlines()[0])
            else:
                return '%s: %s\n' % (
                    self.name, self.template_description())
            # @@: for verbosity >= 2 we should give lots of metadata
            any = True
        if not any:
            print 'No application templates found'

    def template_description(self):
        f = open(os.path.join(self.template_dir, 'description.txt'))
        content = f.read().strip()
        f.close()
        return content

class CommandServe(Command):

    name = 'serve'
    summary = 'Run server'
    parser = standard_parser(simulate=False)
    parser.add_option(
        '--daemon',
        help="Run in daemon (background) mode",
        dest="daemon",
        action="store_true")
    parser.add_option(
        '--user',
        metavar="USER_OR_UID",
        help="User to run as (only works if started as root)",
        dest="user")
    parser.add_option(
        '--group',
        metavar="GROUP_OR_GID",
        help="Group to run as (only works if started as root)",
        dest="group")
    parser.add_option(
        '--pid-file',
        metavar="FILENAME",
        help="Write PID to FILENAME",
        dest="pid_file")
    parser.add_option(
        '--log-file',
        metavar="FILENAME",
        help="File to write stdout/stderr to (other log files may still be generated by the application)",
        dest="log_file")
    parser.add_option(
        '--server',
        metavar='SERVER_NAME',
        help="The kind of server to start",
        dest="server")

    def command(self):
        conf = self.config
        verbose = conf.get('verbose') or 0
        if conf.get('daemon'):
            # We must enter daemon mode!
            if verbose > 1:
                print 'Entering daemon mode'
            pid = os.fork()
            if pid:
                sys.exit()
            # Always record PID and output when daemonized
            if not conf.get('pid_file'):
                conf['pid_file'] = 'server.pid'
            if not conf.get('log_file'):
                conf['log_file'] = 'server.log'
        if conf.get('pid_file'):
            # @@: We should check if the pid file exists and has
            # an active process in it
            if verbose > 1:
                print 'Writing pid %s to %s' % (os.getpid(), conf['pid_file'])
            f = open(conf['pid_file'], 'w')
            f.write(str(os.getpid()))
            f.close()
        if conf.get('log_file'):
            if verbose > 1:
                print 'Logging to %s' % conf['log_file']
            f = open(conf['log_file'], 'a', 1) # 1==line buffered
            sys.stdout = sys.stderr = f
            f.write('-'*20
                    + ' Starting server PID: %s ' % os.getpid()
                    + '-'*20 + '\n')
        self.change_user_group(conf.get('user'), conf.get('group'))
        sys.exit(server.run_server(conf, self.app))

    def change_user_group(self, user, group):
        if not user and not group:
            return
        uid = gid = None
        if group:
            try:
                gid = int(group)
            except ValueError:
                import grp
                try:
                    entry = grp.getgrnam(group)
                except KeyError:
                    raise KeyError(
                        "Bad group: %r; no such group exists" % group)
                gid = entry[2]
        try:
            uid = int(user)
        except ValueError:
            import pwd
            try:
                entry = pwd.getpwnam(user)
            except KeyError:
                raise KeyError(
                    "Bad username: %r; no such user exists" % user)
            if not gid:
                gid = entry[3]
            uid = entry[2]
        if gid:
            os.setgid(gid)
        if uid:
            os.setuid(uid)

    def parse_args(self, args):
        # Unlike most commands, this takes arbitrary options and folds
        # them into the configuration
        conf, app = server.load_commandline(args)
        if conf is None:
            sys.exit(app)
        if app == 'help':
            self.help(conf)
            sys.exit()
        if conf.get('list_servers'):
            self.list_servers(conf)
            sys.exit()
        CONFIG.push_process_config(conf)
        self.config = conf
        self.options = None
        self.args = []
        self.app = app

    def help(self, config):
        # Here we make a fake parser just to get the help
        parser = optparse.OptionParser()
        group = parser.add_option_group("general options")
        group.add_options(server.load_commandline_options())
        extra_help = None
        if config.get('server'):
            try:
                server_mod = server.get_server_mod(config['server'])
            except plugin.PluginNotFound, e:
                print "Server %s not found" % config['server']
                print "  (%s)" % e
                sys.exit(1)
            ops = getattr(server_mod, 'options', None)
            if ops:
                group = parser.add_option_group(
                    "%s options" % server_mod.plugin_name,
                    description=getattr(server_mod, 'description', None))
                group.add_options(ops)
            extra_help = getattr(server_mod, 'help', None)
        parser.print_help()
        if extra_help:
            print
            # @@: textwrap kills any special formatting, so maybe
            # we just can't use it
            #print self.fill_text(extra_help)
            print extra_help

    def list_servers(self, config):
        server_ops = plugin.find_plugins('servers', '_server')
        server_ops.sort()
        print 'These servers are available:'
        print
        for server_name in server_ops:
            self.show_server(server_name)

    def show_server(self, server_name):
        server_mod = server.get_server_mod(server_name)
        print '%s:' % server_mod.plugin_name
        desc = getattr(server_mod, 'description', None)
        if not desc:
            print '    No description available'
        else:
            print self.fill_text(desc)

    def fill_text(self, text):
        try:
            width = int(os.environ['COLUMNS'])
        except (KeyError, ValueError):
            width = 80
        width -= 2
        return textwrap.fill(
            text,
            width,
            initial_indent=' '*4,
            subsequent_indent=' '*4)
        

class CommandHelp(Command):

    name = 'help'
    summary = 'Show help'

    parser = standard_parser(verbose=False)

    max_args = 1

    def command(self):
        if self.args:
            self.runner.run([self.invoked_as, self.args[0], '-h'],
                            self.template_name, self.template_dir,
                            self.template_module,
                            self.runner.server_conf_fn)
        else:
            print 'Available commands:'
            print '  (use "%s help COMMAND" or "%s COMMAND -h" ' % (
                self.prog_name, self.prog_name)
            print '  for more information)'
            items = self.runner.commands.items()
            items.sort()
            max_len = max([len(cn) for cn, c in items])
            for command_name, command in items:
                print '%s:%s %s' % (command_name,
                                    ' '*(max_len-len(command_name)),
                                    command.summary)
                if command.aliases:
                    print '%s (Aliases: %s)' % (
                        ' '*max_len, ', '.join(command.aliases))

class CommandInstall(Command):

    name = 'install'
    summary = 'Install software and enable plugins'

    parser = standard_parser(simulate=False, interactive=False)
    parser.add_option(
        '--enable',
        help="Enable the Paste plugin in the given package (don't install)",
        action="store_true",
        dest="enable")
    parser.add_option(
        '-d', '--install-dir',
        metavar="DIR",
        help="Directory to install into (default: site-packages)",
        dest="install_dir")
    parser.add_option(
        '-s', '--site-packages',
        help="Install in site-packages (install globally)",
        action="store_true",
        dest="site_packages")

    max_args = 1
    min_args = 1

    def command(self):
        global easy_install
        import easy_install
        self.conf = pyconfig.Config(with_default=True)
        self.server_conf_fn = self.runner.server_conf_fn
        if self.server_conf_fn:
            self.conf.load(self.server_conf_fn)
        spec = self.args[0]
        if not self.options.enable:
            packages = self.distribution_packages(self.install(spec))
        else:
            if self.options.install_dir:
                raise InvalidCommand(
                    "You cannot provide the --install-dir option with --enable")
            packages = [spec]
        for pkg in packages:
            self.enable(pkg)

    def install(self, spec):
        v = self.options.verbose
        instdir = self.options.install_dir
        if instdir is None:
            instdir = self.conf.get('app_packages', 'app-packages')
            instdir = os.path.join(os.path.dirname(self.server_conf_fn),
                                   instdir)
        if self.options.site_packages:
            inst_dir = None
        if instdir and instdir not in sys.path:
            sys.path.install(0, instdir)
        if instdir and not os.path.exists(instdir):
            os.makedirs(instdir)
        urlspec = easy_install.URL_SCHEME(spec)
        if not urlspec:
            spec = self.conf['package_urls'].get(spec, spec)
        installer = easy_install.Installer(instdir)
        try:
            if v:
                print 'Acquiring', spec
            downloaded = installer.download(spec)
            installed = installer.install_eggs(downloaded)
        finally:
            installer.close()
        if v:
            print 'Installed into', (
                ', '.join([i.path for i in installed]))
        return installed

    def enable(self, package):
        v = self.options.verbose
        pkg_mod = import_string.import_module(package)
        pkg_dir = os.path.dirname(pkg_mod.__file__)
        installer_fn = os.path.join(pkg_dir, 'paste_install.py')
        if not os.path.exists(installer_fn):
            if self.options.enable:
                raise InvalidCommand(
                    "The package %s cannot be enabled (the modules "
                    "%s doesn't exist)" % (package, installer_fn))
            if v:
                print '%s does not exist; not enabling %s' % (
                    installer_fn, package)
            return
        installer_mod = import_string.import_module(
            package + '.paste_install')
        installer_mod.install_in_paste(
            os.path.dirname(server.__file__))
        if v:
            print '%s enabled' % package

    def distribution_packages(self, dists):
        pkgs = []
        for dist in dists:
            for fn in os.listdir(dist.path):
                if os.path.exists(
                    os.path.join(dist.path, fn, '__init__.py')):
                    pkgs.append(fn)
        return pkgs

############################################################
## Optional helper commands
############################################################

class CommandCreate(Command):

    name = 'create'
    summary = 'Create application from template'

    max_args = 1
    min_args = 1

    parser = standard_parser()

    default_options = {
        'server': 'wsgiutils',
        'verbose': True,
        'reload': True,
        'debug': True,
        }

    def command(self):
        self.output_dir = self.args[0]
        self.create(self.output_dir)
        if self.options.verbose:
            print 'Now do:'
            print '  cd %s' % self.options.output_dir
            print '  wsgi-server'

    def create(self, output_dir):
        file_dir = self.file_source_dir()
        if not os.path.exists(file_dir):
            raise OSError(
                'No %s directory, I don\'t know what to do next' % file_dir)
        template_options = self.default_options.copy()
        template_options.update(self.options.__dict__)
        template_options['app_name'] = os.path.basename(output_dir)
        template_options['base_dir'] = output_dir
        template_options['absolute_base_dir'] = os.path.abspath(output_dir)
        template_options['absolute_parent'] = os.path.dirname(
            os.path.abspath(output_dir))
        template_options['template_name'] = self.template_name
        self.copy_dir(file_dir, output_dir, template_options,
                      self.options.verbose, self.options.simulate)

    def file_source_dir(self):
        return os.path.join(self.template_dir, 'template')

    def copy_dir(self, *args, **kw):
        copy_dir(*args, **kw)

def copy_dir(source, dest, vars, verbosity, simulate):
    names = os.listdir(source)
    names.sort()
    if not os.path.exists(dest):
        if verbosity >= 1:
            print 'Creating %s/' % dest
        if not simulate:
            os.makedirs(dest)
    elif verbosity >= 2:
        print 'Directory %s exists' % dest
    for name in names:
        full = os.path.join(source, name)
        if name.startswith('.'):
            if verbosity >= 2:
                print 'Skipping hidden file %s' % full
            continue
        dest_full = os.path.join(dest, _substitute_filename(name, vars))
        if dest_full.endswith('_tmpl'):
            dest_full = dest_full[:-5]
        if os.path.isdir(full):
            if verbosity:
                print 'Recursing into %s' % full
            copy_dir(full, dest_full, vars, verbosity, simulate)
            continue
        f = open(full, 'rb')
        content = f.read()
        f.close()
        content = _substitute_content(content, vars)
        if verbosity:
            print 'Copying %s to %s' % (full, dest_full)
        f = open(dest_full, 'wb')
        f.write(content)
        f.close()

def _substitute_filename(fn, vars):
    for var, value in vars.items():
        fn = fn.replace('+%s+' % var, str(value))
    return fn

def _substitute_content(content, vars):
    tmpl = string.Template(content)
    return tmpl.substitute(TypeMapper(vars))

class TypeMapper(dict):

    def __getitem__(self, item):
        if item.startswith('str_'):
            return repr(str(self[item[4:]]))
        elif item.startswith('bool_'):
            if self[item[5:]]:
                return 'True'
            else:
                return 'False'
        else:
            return dict.__getitem__(self, item)

if __name__ == '__main__':
    run(sys.argv)

from collections import defaultdict
from itertools import ifilterfalse, chain, groupby
from operator import attrgetter, itemgetter

from snakeoil import mappings
from snakeoil.demandload import demandload

from pkgcheck import base, addons

demandload(
    'os',
    'snakeoil.contexts:patch',
    'snakeoil.osutils:listdir_dirs,pjoin',
    'snakeoil.sequences:iflatten_instance',
    'snakeoil.strings:pluralism',
    'pkgcore.ebuild:atom,misc',
    'pkgcore.ebuild.profiles:ProfileNode,ProfileStack',
    'pkgcore:fetch',
)


class MultiMovePackageUpdate(base.Warning):
    """Entry for package moved multiple times in profiles/updates files."""

    __slots__ = ("pkg", "moves")

    threshold = base.repository_feed

    def __init__(self, pkg, moves):
        super(MultiMovePackageUpdate, self).__init__()
        self.pkg = str(pkg)
        self.moves = tuple([self.pkg] + list(map(str, moves)))

    @property
    def short_desc(self):
        return "'%s': multi-move update: %s" % (
            self.pkg, ' -> '.join(self.moves))


class OldMultiMovePackageUpdate(MultiMovePackageUpdate):
    """Old entry for removed package moved multiple times in profiles/updates files.

    This means that the reported pkg has been moved at least three times and
    finally removed from the tree. All the related lines should be removed from
    the update files.
    """

    __slots__ = ("pkg", "moves")

    threshold = base.repository_feed

    def __init__(self, pkg, moves):
        super(MultiMovePackageUpdate, self).__init__()
        self.pkg = str(moves[-1])
        self.moves = tuple([str(pkg)] + list(map(str, moves)))

    @property
    def short_desc(self):
        return "'%s' unavailable: old multi-move update: %s" % (
            self.pkg, ' -> '.join(self.moves))


class OldPackageUpdate(base.Warning):
    """Old entry for removed package in profiles/updates files."""

    __slots__ = ("pkg", "updates")

    threshold = base.repository_feed

    def __init__(self, pkg, updates):
        super(OldPackageUpdate, self).__init__()
        self.pkg = pkg
        self.updates = tuple(map(str, updates))

    @property
    def short_desc(self):
        return "'%s' unavailable: old update line: '%s'" % (
            self.pkg, ' '.join(self.updates))


class MovedPackageUpdate(base.Warning):
    """Entry for package already moved in profiles/updates files."""

    __slots__ = ("error",)

    threshold = base.repository_feed

    def __init__(self, error):
        super(MovedPackageUpdate, self).__init__()
        self.error = error

    @property
    def short_desc(self):
        return self.error


class BadPackageUpdate(base.Error):
    """Badly formatted package update in profiles/updates files."""

    __slots__ = ("error",)

    threshold = base.repository_feed

    def __init__(self, error):
        super(BadPackageUpdate, self).__init__()
        self.error = error

    @property
    def short_desc(self):
        return self.error


class PackageUpdatesCheck(base.Template):
    """Scan profiles/updates/* for outdated entries and other issues."""

    feed_type = base.repository_feed
    scope = base.repository_scope
    known_results = (
        MultiMovePackageUpdate, OldMultiMovePackageUpdate,
        OldPackageUpdate, MovedPackageUpdate, BadPackageUpdate,
    )

    def __init__(self, options):
        super(PackageUpdatesCheck, self).__init__(options)
        self.repo = options.target_repo

    def feed(self, pkg, reporter):
        pass

    def finish(self, reporter):
        report_bad_updates = lambda x: reporter.add_report(BadPackageUpdate(x))
        report_old_updates = lambda x: reporter.add_report(MovedPackageUpdate(x))

        # convert log warnings/errors into reports
        with patch('pkgcore.log.logger.error', report_bad_updates), \
                patch('pkgcore.log.logger.warning', report_old_updates):
            repo_updates = self.repo.config.updates

        multi_move_updates = {}
        old_move_updates = {}
        old_slotmove_updates = {}

        for pkg, updates in repo_updates.iteritems():
            move_updates = [x for x in updates if x[0] == 'move']
            slotmove_updates = [x for x in updates if x[0] == 'slotmove']

            # check for multi-updates, a -> b, b -> c, ...
            if len(move_updates) > 1:
                # the most recent move should override all the older entries,
                # meaning only a single report for the entire chain should created
                multi_move_updates[move_updates[-1][2]] = (pkg, [x[2] for x in move_updates])
            else:
                # scan updates for old entries with removed packages
                for x in move_updates:
                    _, _old, new = x
                    if not self.repo.match(new):
                        old_move_updates[new] = x

            # scan updates for old entries with removed packages
            for x in slotmove_updates:
                _, pkg, newslot = x
                if not self.repo.match(atom.atom(pkg.key)):
                    # reproduce updates file line data for result output
                    x = ('slotmove', str(pkg)[:-(len(pkg.slot) + 1)], pkg.slot, newslot)
                    old_slotmove_updates[pkg.key] = x

        for pkg, v in multi_move_updates.iteritems():
            orig_pkg, moves = v
            # check for multi-move chains ending in removed packages
            if not self.repo.match(pkg):
                reporter.add_report(OldMultiMovePackageUpdate(orig_pkg, moves))
                # don't generate duplicate old report
                old_move_updates.pop(pkg, None)
            else:
                reporter.add_report(MultiMovePackageUpdate(orig_pkg, moves))

        # report remaining old updates
        for pkg, move in chain(old_move_updates.iteritems(), old_slotmove_updates.iteritems()):
            reporter.add_report(OldPackageUpdate(pkg, move))

class UnusedGlobalFlags(base.Warning):
    """Unused use.desc flag(s)."""

    __slots__ = ("flags",)

    threshold = base.repository_feed

    def __init__(self, flags):
        super(UnusedGlobalFlags, self).__init__()
        # tricky, but it works; atoms have the same attrs
        self.flags = tuple(sorted(flags))

    @property
    def short_desc(self):
        return "use.desc unused flag%s: %s" % (
            pluralism(self.flags), ', '.join(self.flags))


class UnusedInMastersGlobalFlags(base.Warning):
    """Global USE flags detected that are unused in the master repo(s).

    In other words, they're likely to be removed so should be copied to the overlay.
    """

    __slots__ = ("category", "package", "version", "flags")

    threshold = base.versioned_feed

    def __init__(self, pkg, flags):
        super(UnusedInMastersGlobalFlags, self).__init__()
        self._store_cpv(pkg)
        self.flags = tuple(sorted(flags))

    @property
    def short_desc(self):
        return "use.desc unused flag%s in master repo(s): %s" % (
            pluralism(self.flags), ', '.join(self.flags))


class UnusedGlobalFlagsCheck(base.Template):
    """Check for unused use.desc entries."""

    feed_type = base.versioned_feed
    scope = base.repository_scope
    required_addons = (addons.UseAddon,)
    known_results = (UnusedGlobalFlags, UnusedInMastersGlobalFlags)

    def __init__(self, options, iuse_handler):
        super(UnusedGlobalFlagsCheck, self).__init__(options)
        self.unused_flags = None
        self.iuse_handler = iuse_handler

    def start(self):
        master_flags = self.unused_master_flags = set()
        for repo in self.options.target_repo.masters:
            master_flags.update(x[1][0] for x in repo.config.use_desc)
            master_flags.update(x[1][0] for x in repo.config.use_expand_desc)
        self.unused_flags = set(self.iuse_handler.global_iuse - self.iuse_handler.unstated_iuse) - master_flags

        # determine unused flags across all master repos
        if master_flags:
            for repo in self.options.target_repo.masters:
                for pkg in repo:
                    self.unused_master_flags.difference_update(pkg.iuse_stripped)

    def feed(self, pkg, reporter):
        self.unused_flags.difference_update(pkg.iuse_stripped)

        # report flags used in the pkg but not in any pkg from the master repo(s)
        if self.unused_master_flags:
            flags = self.unused_master_flags.intersection(pkg.iuse_stripped)
            if flags:
                reporter.add_report(UnusedInMastersGlobalFlags(pkg, flags))

    def finish(self, reporter):
        if self.unused_flags:
            reporter.add_report(UnusedGlobalFlags(self.unused_flags))

        self.unused_flags = self.unused_master_flags = None


class UnusedLicenses(base.Warning):
    """Unused license(s) detected."""

    __slots__ = ("licenses",)

    threshold = base.repository_feed

    def __init__(self, licenses):
        super(UnusedLicenses, self).__init__()
        self.licenses = tuple(sorted(licenses))

    @property
    def short_desc(self):
        return ', '.join(self.licenses)


class UnusedInMastersLicenses(base.Warning):
    """Licenses detected that are unused in the master repo(s).

    In other words, they're likely to be removed so should be copied to the overlay.
    """

    __slots__ = ("category", "package", "version", "licenses")

    threshold = base.versioned_feed

    def __init__(self, pkg, licenses):
        super(UnusedInMastersLicenses, self).__init__()
        self._store_cpv(pkg)
        self.licenses = tuple(sorted(licenses))

    @property
    def short_desc(self):
        return "unused license%s in master repo(s): %s" % (
            pluralism(self.licenses), ', '.join(self.licenses))


class UnusedLicensesCheck(base.Template):
    """Check for unused license files."""

    feed_type = base.versioned_feed
    scope = base.repository_scope
    known_results = (UnusedLicenses, UnusedInMastersLicenses)

    def __init__(self, options):
        super(UnusedLicensesCheck, self).__init__(options)
        self.unused_licenses = None

    def start(self):
        master_licenses = self.unused_master_licenses = set()
        for repo in self.options.target_repo.masters:
            master_licenses.update(repo.licenses)
        self.unused_licenses = set(self.options.target_repo.licenses) - master_licenses

        # determine unused licenses across all master repos
        self.unused_in_master_licenses = set()
        if master_licenses:
            for repo in self.options.target_repo.masters:
                for pkg in repo:
                    self.unused_master_licenses.difference_update(iflatten_instance(pkg.license))

    def feed(self, pkg, reporter):
        pkg_licenses = set(iflatten_instance(pkg.license))
        self.unused_licenses.difference_update(pkg_licenses)

        # report licenses used in the pkg but not in any pkg from the master repo(s)
        if self.unused_master_licenses:
            licenses = self.unused_master_licenses & pkg_licenses
            if licenses:
                reporter.add_report(UnusedInMastersLicenses(pkg, licenses))

    def finish(self, reporter):
        if self.unused_licenses:
            reporter.add_report(UnusedLicenses(self.unused_licenses))

        self.unused_licenses = self.unused_master_licenses = None


class UnusedMirrors(base.Warning):
    """Unused mirrors detected."""

    __slots__ = ("mirrors",)

    threshold = base.repository_feed

    def __init__(self, mirrors):
        super(UnusedMirrors, self).__init__()
        self.mirrors = tuple(sorted(mirrors))

    @property
    def short_desc(self):
        return ', '.join(self.mirrors)


class UnusedInMastersMirrors(base.Warning):
    """Mirrors detected that are unused in the master repo(s).

    In other words, they're likely to be removed so should be copied to the overlay.
    """

    __slots__ = ("category", "package", "version", "mirrors")

    threshold = base.versioned_feed

    def __init__(self, pkg, mirrors):
        super(UnusedInMastersMirrors, self).__init__()
        self._store_cpv(pkg)
        self.mirrors = tuple(sorted(mirrors))

    @property
    def short_desc(self):
        return "unused mirror%s in master repo(s): %s" % (
            pluralism(self.mirrors), ', '.join(self.mirrors))


class UnusedMirrorsCheck(base.Template):
    """Check for unused mirrors."""

    required_addons = (addons.UseAddon,)
    feed_type = base.versioned_feed
    scope = base.repository_scope
    known_results = (UnusedMirrors, UnusedInMastersMirrors)

    def __init__(self, options, iuse_handler):
        super(UnusedMirrorsCheck, self).__init__(options)
        self.unused_mirrors = None
        self.iuse_filter = iuse_handler.get_filter('fetchables')

    def _get_mirrors(self, pkg, reporter=None):
        mirrors = []
        for f in self.iuse_filter((fetch.fetchable,), pkg, pkg.fetchables):
            for m in f.uri.visit_mirrors(treat_default_as_mirror=False):
                mirrors.append(m[0].mirror_name)
        return set(mirrors)

    def start(self):
        master_mirrors = self.unused_master_mirrors = set()
        for repo in self.options.target_repo.masters:
            master_mirrors.update(repo.mirrors.iterkeys())
        self.unused_mirrors = set(self.options.target_repo.mirrors.iterkeys()) - master_mirrors

        # determine unused mirrors across all master repos
        self.unused_in_master_mirrors = set()
        if master_mirrors:
            for repo in self.options.target_repo.masters:
                for pkg in repo:
                    self.unused_master_mirrors.difference_update(self._get_mirrors(pkg))

    def feed(self, pkg, reporter):
        pkg_mirrors = self._get_mirrors(pkg)
        if self.unused_mirrors:
            self.unused_mirrors.difference_update(pkg_mirrors)

        # report mirrors used in the pkg but not in any pkg from the master repo(s)
        if self.unused_master_mirrors:
            mirrors = self.unused_master_mirrors & pkg_mirrors
            if mirrors:
                reporter.add_report(UnusedInMastersMirrors(pkg, mirrors))

    def finish(self, reporter):
        if self.unused_mirrors:
            reporter.add_report(UnusedMirrors(self.unused_mirrors))

        self.unused_mirrors = self.unused_master_mirrors = None


class UnusedEclasses(base.Warning):
    """Unused eclasses detected."""

    __slots__ = ("eclasses",)

    threshold = base.repository_feed

    def __init__(self, eclasses):
        super(UnusedEclasses, self).__init__()
        self.eclasses = tuple(sorted(eclasses))

    @property
    def short_desc(self):
        return ', '.join(self.eclasses)


class UnusedInMastersEclasses(base.Warning):
    """Eclasses detected that are unused in the master repo(s).

    In other words, they're likely to be removed so should be copied to the overlay.
    """

    __slots__ = ("category", "package", "version", "eclasses")

    threshold = base.versioned_feed

    def __init__(self, pkg, eclasses):
        super(UnusedInMastersEclasses, self).__init__()
        self._store_cpv(pkg)
        self.eclasses = tuple(sorted(eclasses))

    @property
    def short_desc(self):
        return "unused eclass%s in master repo(s): %s" % (
            pluralism(self.eclasses, 'es'), ', '.join(self.eclasses))


class UnusedEclassesCheck(base.Template):
    """Check for unused eclasses."""

    feed_type = base.versioned_feed
    scope = base.repository_scope
    known_results = (UnusedEclasses, UnusedInMastersEclasses)

    def __init__(self, options):
        super(UnusedEclassesCheck, self).__init__(options)
        self.unused_eclasses = None

    def start(self):
        master_eclasses = self.unused_master_eclasses = set()
        for repo in self.options.target_repo.masters:
            master_eclasses.update(repo.eclass_cache.eclasses.iterkeys())
        self.unused_eclasses = set(self.options.target_repo.eclass_cache.eclasses.iterkeys()) - master_eclasses

        # determine unused eclasses across all master repos
        self.unused_in_master_eclasses = set()
        if master_eclasses:
            for repo in self.options.target_repo.masters:
                for pkg in repo:
                    self.unused_master_eclasses.difference_update(pkg.inherited)

    def feed(self, pkg, reporter):
        pkg_eclasses = set(pkg.inherited)
        self.unused_eclasses.difference_update(pkg_eclasses)

        # report eclasses used in the pkg but not in any pkg from the master repo(s)
        if self.unused_master_eclasses:
            eclasses = self.unused_master_eclasses & pkg_eclasses
            if eclasses:
                reporter.add_report(UnusedInMastersEclasses(pkg, eclasses))

    def finish(self, reporter):
        if self.unused_eclasses:
            reporter.add_report(UnusedEclasses(self.unused_eclasses))

        self.unused_eclasses = self.unused_master_eclasses = None


class UnusedProfileDirs(base.Warning):
    """Unused profile directories detected."""

    __slots__ = ("dirs",)

    threshold = base.repository_feed

    def __init__(self, dirs):
        super(UnusedProfileDirs, self).__init__()
        self.dirs = tuple(sorted(dirs))

    @property
    def short_desc(self):
        return "[ %s ]" % ', '.join(self.dirs)


class UnknownProfileArches(base.Warning):
    """Unknown arches used in profiles."""

    __slots__ = ("arches",)

    threshold = base.repository_feed

    def __init__(self, arches):
        super(UnknownProfileArches, self).__init__()
        self.arches = arches

    @property
    def short_desc(self):
        return "[ %s ]" % ', '.join(self.arches)


class ArchesWithoutProfiles(base.Warning):
    """Arches without corresponding profile listings."""

    __slots__ = ("arches",)

    threshold = base.repository_feed

    def __init__(self, arches):
        super(ArchesWithoutProfiles, self).__init__()
        self.arches = arches

    @property
    def short_desc(self):
        return "[ %s ]" % ', '.join(self.arches)


class UnknownProfileStatus(base.Warning):
    """Unknown status used for profiles."""

    __slots__ = ("status",)

    threshold = base.repository_feed

    def __init__(self, status):
        super(UnknownProfileStatus, self).__init__()
        self.status = status

    @property
    def short_desc(self):
        return "[ %s ]" % ', '.join(self.status)


class NonexistentProfilePath(base.Warning):
    """Specified profile path doesn't exist."""

    __slots__ = ("path",)

    threshold = base.repository_feed

    def __init__(self, path):
        super(NonexistentProfilePath, self).__init__()
        self.path = path

    @property
    def short_desc(self):
        return self.path


class UnknownCategories(base.Warning):
    """Category directories that aren't listed in a repo's categories.

    Or the categories of the repo's masters as well.
    """

    __slots__ = ("categories",)

    threshold = base.repository_feed

    def __init__(self, categories):
        super(UnknownCategories, self).__init__()
        self.categories = categories

    @property
    def short_desc(self):
        return "[ %s ]" % ', '.join(self.categories)


class BadProfileEntry(base.Error):
    """Badly formatted entry in a profiles file."""

    __slots__ = ("path", "error")

    threshold = base.repository_feed

    def __init__(self, path, error):
        super(BadProfileEntry, self).__init__()
        self.path = path
        self.error = error

    @property
    def short_desc(self):
        return 'failed parsing %r: %s' % (self.path, self.error)


class UnknownProfilePackages(base.Warning):
    """Profile files include package entries that don't exist in the repo."""

    __slots__ = ("path", "packages")

    threshold = base.repository_feed

    def __init__(self, path, packages):
        super(UnknownProfilePackages, self).__init__()
        self.path = path
        self.packages = tuple(str(x) for x in packages)

    @property
    def short_desc(self):
        return "unknown package%s in %r: [ %s ]" % (
            pluralism(self.packages), self.path, ', '.join(map(repr, self.packages)))


class UnknownProfilePackageUse(base.Warning):
    """Profile files include entries with USE flags that aren't used on any matching packages."""

    __slots__ = ("path", "package", "flags")

    threshold = base.repository_feed

    def __init__(self, path, package, flags):
        super(UnknownProfilePackageUse, self).__init__()
        self.path = path
        self.package = str(package)
        self.flags = tuple(flags)

    @property
    def short_desc(self):
        return "unknown USE flag%s for %r in %r: [ %s ]" % (
            pluralism(self.flags), self.package, self.path,
            ', '.join(map(repr, self.flags)))


class UnknownProfileUse(base.Warning):
    """Profile files include USE flags that don't exist."""

    __slots__ = ("path", "flags")

    threshold = base.repository_feed

    def __init__(self, path, flags):
        super(UnknownProfileUse, self).__init__()
        self.path = path
        self.flags = tuple(flags)

    @property
    def short_desc(self):
        return "unknown USE flag%s in %r: [ %s ]" % (
            pluralism(self.flags), self.path, ', '.join(map(repr, self.flags)))


class ProfilesCheck(base.Template):
    """Scan repo profiles for unknown flags/packages."""

    required_addons = (addons.ProfileAddon, addons.UseAddon)
    feed_type = base.repository_feed
    scope = base.repository_scope
    known_results = (
        BadProfileEntry, UnknownProfilePackages,
        UnknownProfilePackageUse, UnknownProfileUse,
    )

    def __init__(self, options, profile_filters, iuse_handler):
        super(ProfilesCheck, self).__init__(options)
        self.repo = options.target_repo
        self.profiles_dir = pjoin(self.repo.location, 'profiles')
        self.non_profile_dirs = {
            pjoin(self.profiles_dir, x) for x in profile_filters.non_profile_dirs}
        local_iuse = {use for pkg, (use, desc) in self.repo.config.use_local_desc}
        self.available_iuse = frozenset(
            local_iuse | iuse_handler.global_iuse | iuse_handler.unstated_iuse)

    def feed(self, pkg, reporter):
        pass

    def finish(self, reporter):
        unknown_pkgs = defaultdict(lambda: defaultdict(list))
        unknown_pkg_use = defaultdict(lambda: defaultdict(list))
        unknown_use = defaultdict(lambda: defaultdict(list))

        def _pkg_atoms(vals):
            for x in iflatten_instance(vals, atom.atom):
                if not self.repo.match(x):
                    unknown_pkgs[profile.path][filename].append(x)

        def _pkg_use(vals):
            # TODO: give ChunkedDataDict some dict view methods
            d = vals
            if isinstance(d, misc.ChunkedDataDict):
                d = vals.render_to_dict()

            for _pkg, entries in d.iteritems():
                for x, disabled, enabled in entries:
                    pkgs = self.repo.match(x)
                    if not pkgs:
                        unknown_pkgs[profile.path][filename].append(x)
                    else:
                        available = {x for pkg in pkgs for x in pkg.iuse_stripped}
                        requested = set(disabled).union(enabled)
                        unknown = requested - available
                        if unknown:
                            unknown_pkg_use[profile.path][filename].append((x, unknown))

        def _use(vals):
            # TODO: give ChunkedDataDict some dict view methods
            d = vals.render_to_dict()
            for _, entries in d.iteritems():
                for _, disabled, enabled in entries:
                    requested = set(disabled).union(enabled)
                    unknown = requested - self.available_iuse
                    if unknown:
                        unknown_use[profile.path][filename].extend(unknown)

        for root, _dirs, files in os.walk(self.profiles_dir):
            if root not in self.non_profile_dirs:
                profile = ProfileNode(root)
                for filename, attr, func in (
                    ('packages', 'packages', _pkg_atoms),
                    ('package.mask', 'masks', _pkg_atoms),
                    ('package.unmask', 'unmasks', _pkg_atoms),
                    ('package.use', 'pkg_use', _pkg_use),
                    ('package.use.force', 'pkg_use_force', _pkg_use),
                    ('package.use.stable.force', 'pkg_use_stable_force', _pkg_use),
                    ('package.use.mask', 'pkg_use_mask', _pkg_use),
                    ('package.use.stable.mask', 'pkg_use_stable_mask', _pkg_use),
                    ('use.force', 'use_force', _use),
                    ('use.stable.force', 'use_stable_force', _use),
                    ('use.mask', 'use_mask', _use),
                    ('use.stable.mask', 'use_stable_mask', _use)):

                    if filename in files:
                        # catch badly formatted entries
                        # TODO: switch this to a patched logger catcher once
                        # pkgcore is updated to log and ignore bad entries
                        try:
                            vals = getattr(profile, attr)
                        except Exception as e:
                            reporter.add_report(BadProfileEntry(
                                pjoin(root[len(self.profiles_dir):].lstrip('/'), e.filename),
                                e.error))
                            continue

                        func(vals)

        for path, filenames in sorted(unknown_pkgs.iteritems()):
            for filename, vals in filenames.iteritems():
                reporter.add_report(UnknownProfilePackages(
                    pjoin(path[len(self.profiles_dir):].lstrip('/'), filename),
                    vals))

        for path, filenames in sorted(unknown_pkg_use.iteritems()):
            for filename, vals in filenames.iteritems():
                for pkg, flags in vals:
                    reporter.add_report(UnknownProfilePackageUse(
                        pjoin(path[len(self.profiles_dir):].lstrip('/'), filename),
                        pkg, flags))

        for path, filenames in sorted(unknown_use.iteritems()):
            for filename, vals in filenames.iteritems():
                reporter.add_report(UnknownProfileUse(
                    pjoin(path[len(self.profiles_dir):].lstrip('/'), filename),
                    vals))


class RepoProfilesReport(base.Template):
    """Scan repo for various profiles directory issues.

    Including unknown arches in profiles, arches without profiles, and unknown
    categories.
    """

    required_addons = (addons.ProfileAddon,)
    feed_type = base.repository_feed
    scope = base.repository_scope
    known_results = (
        UnknownProfileArches, ArchesWithoutProfiles, UnusedProfileDirs,
        NonexistentProfilePath, UnknownProfileStatus, UnknownCategories)

    def __init__(self, options, profile_filters):
        super(RepoProfilesReport, self).__init__(options)
        self.arches = options.target_repo.config.known_arches
        self.profiles = options.target_repo.config.arch_profiles.itervalues()
        self.repo = options.target_repo
        self.profiles_dir = pjoin(self.repo.location, 'profiles')
        self.non_profile_dirs = profile_filters.non_profile_dirs

    def feed(self, pkg, reporter):
        pass

    def finish(self, reporter):
        category_dirs = set(ifilterfalse(
            self.repo.false_categories.__contains__,
            (x for x in listdir_dirs(self.repo.location) if x[0] != '.')))
        unknown_categories = category_dirs.difference(self.repo.categories)
        if unknown_categories:
            reporter.add_report(UnknownCategories(unknown_categories))

        unknown_arches = self.repo.config.profiles.arches().difference(self.arches)
        arches_without_profiles = self.arches.difference(self.repo.config.profiles.arches())

        if unknown_arches:
            reporter.add_report(UnknownProfileArches(unknown_arches))
        if arches_without_profiles:
            reporter.add_report(ArchesWithoutProfiles(arches_without_profiles))

        root_profile_dirs = {'embedded'}
        available_profile_dirs = set()
        for root, _dirs, _files in os.walk(self.profiles_dir):
            # skip deprecated profiles
            if not os.path.exists(pjoin(root, 'deprecated')):
                d = root[len(self.profiles_dir):].lstrip('/')
                if d:
                    available_profile_dirs.add(d)
        available_profile_dirs -= self.non_profile_dirs | root_profile_dirs

        def parents(path):
            """Yield all directory path parents excluding the root directory.

            Example:
            >>> list(parents('/root/foo/bar/baz'))
            ['root/foo/bar', 'root/foo', 'root']
            """
            path = os.path.normpath(path.strip('/'))
            while path:
                yield path
                dirname, _basename = os.path.split(path)
                path = dirname.rstrip('/')

        seen_profile_dirs = set()
        profile_status = set()
        for path, status in chain.from_iterable(self.profiles):
            for x in ProfileStack(pjoin(self.profiles_dir, path)).stack:
                seen_profile_dirs.update(parents(x.path[len(self.profiles_dir):]))
            if not os.path.exists(pjoin(self.profiles_dir, path)):
                reporter.add_report(NonexistentProfilePath(path))
            profile_status.add(status)

        unused_profile_dirs = available_profile_dirs - seen_profile_dirs
        if unused_profile_dirs:
            reporter.add_report(UnusedProfileDirs(unused_profile_dirs))

        if self.repo.repo_name == 'gentoo':
            accepted_status = ('stable', 'dev', 'exp')
            unknown_status = profile_status.difference(accepted_status)
            if unknown_status:
                reporter.add_report(UnknownProfileStatus(unknown_status))


class UnknownLicenses(base.Warning):
    """License(s) listed in license group(s) that don't exist."""

    __slots__ = ("group", "licenses")

    threshold = base.repository_feed

    def __init__(self, group, licenses):
        super(UnknownLicenses, self).__init__()
        self.group = group
        self.licenses = licenses

    @property
    def short_desc(self):
        return "license group %r has unknown license%s: [ %s ]" % (
            self.group, pluralism(self.licenses), ', '.join(self.licenses))


class LicenseGroupsCheck(base.Template):
    """Scan license groups for unknown licenses."""

    feed_type = base.repository_feed
    scope = base.repository_scope
    known_results = (UnknownLicenses,)

    def __init__(self, options):
        super(LicenseGroupsCheck, self).__init__(options)
        self.repo = options.target_repo

    def feed(self, pkg, reporter):
        pass

    def finish(self, reporter):
        for group, licenses in self.repo.licenses.groups.iteritems():
            unknown_licenses = set(licenses).difference(self.repo.licenses)
            if unknown_licenses:
                reporter.add_report(UnknownLicenses(group, unknown_licenses))


def reformat_chksums(iterable):
    for chf, val1, val2 in iterable:
        if chf == "size":
            yield chf, val1, val2
        else:
            yield chf, "%x" % val1, "%x" % val2


class ConflictingChksums(base.Error):
    """Checksum conflict detected between two files."""

    __slots__ = ("category", "package", "version",
                 "filename", "chksums", "others")

    threshold = base.versioned_feed

    _sorter = staticmethod(itemgetter(0))

    def __init__(self, pkg, filename, chksums, others):
        super(ConflictingChksums, self).__init__()
        self._store_cpv(pkg)
        self.filename = filename
        self.chksums = tuple(sorted(reformat_chksums(chksums), key=self._sorter))
        self.others = tuple(sorted(others))

    @property
    def short_desc(self):
        return "conflicts with (%s) for file %s chksum %s" % (
            ', '.join(self.others), self.filename, self.chksums)


class MissingChksum(base.Warning):
    """A file in the chksum data lacks required checksums."""

    threshold = base.versioned_feed
    __slots__ = ('category', 'package', 'version', 'filename', 'missing',
                 'existing')

    def __init__(self, pkg, filename, missing, existing):
        super(MissingChksum, self).__init__()
        self._store_cpv(pkg)
        self.filename, self.missing = filename, tuple(sorted(missing))
        self.existing = tuple(sorted(existing))

    @property
    def short_desc(self):
        return '"%s" missing required chksums: %s; has chksums: %s' % \
            (self.filename, ', '.join(self.missing), ', '.join(self.existing))


class MissingManifest(base.Error):
    """SRC_URI targets missing from Manifest file."""

    __slots__ = ("category", "package", "version", "files")
    threshold = base.versioned_feed

    def __init__(self, pkg, files):
        super(MissingManifest, self).__init__()
        self._store_cpv(pkg)
        self.files = tuple(sorted(files))

    @property
    def short_desc(self):
        return "distfile%s missing from Manifest: [ %s ]" % (
            pluralism(self.files), ', '.join(self.files),)


class UnknownManifest(base.Warning):
    """Manifest entries not matching any SRC_URI targets."""

    __slots__ = ("category", "package", "files")
    threshold = base.package_feed

    def __init__(self, pkg, files):
        super(UnknownManifest, self).__init__()
        self._store_cp(pkg)
        self.files = tuple(sorted(files))

    @property
    def short_desc(self):
        return "unknown distfile%s in Manifest: [ %s ]" % (
            pluralism(self.files), ', '.join(self.files),)


class UnnecessaryManifest(base.Warning):
    """Manifest entries for non-DIST targets on a repo with thin manifests enabled."""

    __slots__ = ("category", "package", "files")
    threshold = base.package_feed

    def __init__(self, pkg, files):
        super(UnnecessaryManifest, self).__init__()
        self._store_cp(pkg)
        self.files = tuple(sorted(files))

    @property
    def short_desc(self):
        return "unnecessary file%s in Manifest: [ %s ]" % (
            pluralism(self.files), ', '.join(self.files),)


class ManifestReport(base.Template):
    """Manifest related checks.

    Verify that the Manifest file exists, doesn't have missing or
    extraneous entries, and that the required hashes are in use.
    """

    required_addons = (addons.UseAddon,)
    feed_type = base.package_feed
    known_results = (
        MissingChksum, MissingManifest, UnknownManifest, UnnecessaryManifest,
        ConflictingChksums,
    )

    repo_grabber = attrgetter("repo")

    def __init__(self, options, iuse_handler):
        super(ManifestReport, self).__init__(options)
        self.required_checksums = mappings.defaultdictkey(lambda repo: frozenset(
            repo.config.manifests.hashes if hasattr(repo, 'config') else ()))
        self.seen_checksums = {}
        self.iuse_filter = iuse_handler.get_filter('fetchables')

    def feed(self, full_pkgset, reporter):
        # sort it by repo.
        for repo, pkgset in groupby(full_pkgset, self.repo_grabber):
            required_checksums = self.required_checksums[repo]
            pkgset = list(pkgset)
            pkg_manifest = pkgset[0].manifest
            manifest_distfiles = set(pkg_manifest.distfiles.iterkeys())
            seen = set()
            for pkg in pkgset:
                pkg.release_cached_data()
                fetchables = set(self.iuse_filter(
                    (fetch.fetchable,), pkg,
                    pkg._get_attr['fetchables'](
                        pkg, allow_missing_checksums=True, ignore_unknown_mirrors=True)))
                pkg.release_cached_data()

                fetchable_files = set(f.filename for f in fetchables)
                missing_manifests = fetchable_files.difference(manifest_distfiles)
                if missing_manifests:
                    reporter.add_report(MissingManifest(pkg, missing_manifests))

                for f_inst in fetchables:
                    if f_inst.filename in seen:
                        continue
                    missing = required_checksums.difference(f_inst.chksums)
                    if f_inst.filename not in missing_manifests and missing:
                        reporter.add_report(MissingChksum(
                            pkg, f_inst.filename, missing, f_inst.chksums))
                    seen.add(f_inst.filename)
                    existing = self.seen_checksums.get(f_inst.filename)
                    if existing is None:
                        self.seen_checksums[f_inst.filename] = (
                            [pkg.key], dict(f_inst.chksums.iteritems()))
                        continue
                    seen_pkgs, seen_chksums = existing
                    confl_checksums = []
                    for chf_type, value in seen_chksums.iteritems():
                        our_value = f_inst.chksums.get(chf_type)
                        if our_value is not None and our_value != value:
                            confl_checksums.append((chf_type, value, our_value))
                    if confl_checksums:
                        reporter.add_report(ConflictingChksums(
                            pkg, f_inst.filename, confl_checksums, seen_pkgs))
                    else:
                        seen_chksums.update(f_inst.chksums)
                        seen_pkgs.append(pkg)

            if pkg_manifest.thin:
                unnecessary_manifests = []
                for attr in ('aux_files', 'ebuilds', 'misc'):
                    unnecessary_manifests.extend(getattr(pkg_manifest, attr, []))
                if unnecessary_manifests:
                    reporter.add_report(UnnecessaryManifest(pkgset[0], unnecessary_manifests))

            unknown_manifests = manifest_distfiles.difference(seen)
            if unknown_manifests:
                reporter.add_report(UnknownManifest(pkgset[0], unknown_manifests))
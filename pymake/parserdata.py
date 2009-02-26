import logging, re
import data, functions, util, parser
from cStringIO import StringIO
from pymake.globrelative import hasglob, glob

_log = logging.getLogger('pymake.data')
_tabwidth = 4

def _charlocation(start, char):
    """
    Return the column position after processing a perhaps-tab character.
    This function is meant to be used with reduce().
    """
    if char != '\t':
        return start + 1

    return start + _tabwidth - start % _tabwidth

class Location(object):
    """
    A location within a makefile.

    For the moment, locations are just path/line/column, but in the future
    they may reference parent locations for more accurate "included from"
    or "evaled at" error reporting.
    """
    __slots__ = ('path', 'line', 'column')

    def __init__(self, path, line, column):
        self.path = path
        self.line = line
        self.column = column

    def __add__(self, data):
        """
        Returns a new location on the same line offset by
        the specified string.
        """
        newcol = reduce(_charlocation, data, self.column)
        if newcol == self.column:
            return self
        return Location(self.path, self.line, newcol)

    def __str__(self):
        return "%s:%s:%s" % (self.path, self.line, self.column)

def _expandwildcards(makefile, tlist):
    for t in tlist:
        if not hasglob(t):
            yield t
        else:
            l = glob(makefile.workdir, t)
            for r in l:
                yield r

def parsecommandlineargs(args):
    """
    Given a set of arguments from a command-line invocation of make,
    parse out the variable definitions and return (stmts, arglist)
    """

    stmts = StatementList()
    r = []
    for i in xrange(0, len(args)):
        a = args[i]

        vname, t, val = a.partition(':=')
        if t == '':
            vname, t, val = a.partition('=')
        if t != '':
            stmts.append(Override(a))

            vname = vname.strip()
            vnameexp = data.Expansion.fromstring(vname)

            stmts.append(SetVariable(vnameexp, token=t,
                                     value=val, valueloc=Location('<command-line>', i, len(vname) + len(t)),
                                     targetexp=None, source=data.Variables.SOURCE_COMMANDLINE))
        else:
            r.append(a)

    return stmts, r

class Statement(object):
    """
    A statement is an abstract object representing a single "chunk" of makefile syntax. Subclasses
    must implement the following method:

    def execute(self, makefile, context)
    """

class Override(Statement):
    def __init__(self, s):
        self.s = s

    def execute(self, makefile, context):
        makefile.overrides.append(self.s)

    def dump(self, fd, indent):
        print >>fd, indent, "Override: %r" % (self.s,)

class DummyRule(object):
    def addcommand(self, r):
        _log.debug("Discarding rule at %s" % (r.loc,))
        pass

class Rule(Statement):
    def __init__(self, targetexp, depexp, doublecolon):
        assert isinstance(targetexp, data.Expansion)
        assert isinstance(depexp, data.Expansion)
        
        self.targetexp = targetexp
        self.depexp = depexp
        self.doublecolon = doublecolon

    def execute(self, makefile, context):
        atargets = data.splitwords(self.targetexp.resolve(makefile, makefile.variables))
        targets = [data.Pattern(p) for p in _expandwildcards(makefile, atargets)]

        if not len(targets):
            context.currule = DummyRule()
            return

        ispatterns = set((t.ispattern() for t in targets))
        if len(ispatterns) == 2:
            raise data.DataError("Mixed implicit and normal rule", self.targetexp.loc)
        ispattern, = ispatterns

        deps = [p for p in _expandwildcards(makefile, data.splitwords(self.depexp.resolve(makefile, makefile.variables)))]
        if ispattern:
            rule = data.PatternRule(targets, map(data.Pattern, deps), self.doublecolon, loc=self.targetexp.loc)
            makefile.appendimplicitrule(rule)
        else:
            rule = data.Rule(deps, self.doublecolon, loc=self.targetexp.loc)
            for t in targets:
                makefile.gettarget(t.gettarget()).addrule(rule)
            makefile.foundtarget(targets[0].gettarget())

        context.currule = rule

    def dump(self, fd, indent):
        print >>fd, indent, "Rule %s: %s" % (self.targetexp, self.depexp)

class StaticPatternRule(Statement):
    def __init__(self, targetexp, patternexp, depexp, doublecolon):
        assert isinstance(targetexp, data.Expansion)
        assert isinstance(patternexp, data.Expansion)
        assert isinstance(depexp, data.Expansion)

        self.targetexp = targetexp
        self.patternexp = patternexp
        self.depexp = depexp
        self.doublecolon = doublecolon

    def execute(self, makefile, context):
        targets = list(_expandwildcards(makefile, data.splitwords(self.targetexp.resolve(makefile, makefile.variables))))

        if not len(targets):
            context.currule = DummyRule()
            return

        patterns = data.splitwords(self.patternexp.resolve(makefile, makefile.variables))
        if len(patterns) != 1:
            raise data.DataError("Static pattern rules must have a single pattern", self.patternexp.loc)
        pattern = data.Pattern(patterns[0])

        deps = [data.Pattern(p) for p in _expandwildcards(makefile, data.splitwords(self.depexp.resolve(makefile, makefile.variables)))]

        rule = data.PatternRule([pattern], deps, self.doublecolon, loc=self.targetexp.loc)

        for t in targets:
            if data.Pattern(t).ispattern():
                raise data.DataError("Target '%s' of a static pattern rule must not be a pattern" % (t,), self.targetexp.loc)
            stem = pattern.match(t)
            if stem is None:
                raise data.DataError("Target '%s' does not match the static pattern '%s'" % (t, pattern), self.targetexp.loc)
            makefile.gettarget(t).addrule(data.PatternRuleInstance(rule, '', stem, pattern.ismatchany()))

        makefile.foundtarget(targets[0])
        context.currule = rule

    def dump(self, fd, indent):
        print >>fd, indent, "StaticPatternRule %r: %r: %r" % (self.targetexp, self.patternexp, self.depexp)

class Command(Statement):
    def __init__(self, exp):
        assert isinstance(exp, data.Expansion)
        self.exp = exp

    def execute(self, makefile, context):
        assert context.currule is not None
        context.currule.addcommand(self.exp)

    def dump(self, fd, indent):
        print >>fd, indent, "Command %r" % (self.exp,)

class SetVariable(Statement):
    def __init__(self, vnameexp, token, value, valueloc, targetexp, source=None):
        assert isinstance(vnameexp, data.Expansion)
        assert isinstance(value, str)
        assert targetexp is None or isinstance(targetexp, data.Expansion)

        if source is None:
            source = data.Variables.SOURCE_MAKEFILE

        self.vnameexp = vnameexp
        self.token = token
        self.value = value
        self.valueloc = valueloc
        self.targetexp = targetexp
        self.source = source

    def execute(self, makefile, context):
        vname = self.vnameexp.resolve(makefile, makefile.variables)
        if len(vname) == 0:
            raise data.DataError("Empty variable name", self.vnameexp.loc)

        if self.targetexp is None:
            setvariables = [makefile.variables]
        else:
            setvariables = []

            targets = [data.Pattern(t) for t in data.splitwords(self.targetexp.resolve(makefile, makefile.variables))]
            for t in targets:
                if t.ispattern():
                    setvariables.append(makefile.getpatternvariables(t))
                else:
                    setvariables.append(makefile.gettarget(t.gettarget()).variables)

        for v in setvariables:
            if self.token == '+=':
                v.append(vname, self.source, self.value, makefile.variables, makefile)
                continue

            if self.token == '?=':
                flavor = data.Variables.FLAVOR_RECURSIVE
                oldflavor, oldsource, oldval = v.get(vname, expand=False)
                if oldval is not None:
                    continue
                value = self.value
            elif self.token == '=':
                flavor = data.Variables.FLAVOR_RECURSIVE
                value = self.value
            else:
                assert self.token == ':='

                flavor = data.Variables.FLAVOR_SIMPLE
                d = parser.Data.fromstring(self.value, self.valueloc)
                e, t, o = parser.parsemakesyntax(d, 0, (), parser.iterdata)
                value = e.resolve(makefile, makefile.variables)

            v.set(vname, flavor, self.source, value)

    def dump(self, fd, indent):
        print >>fd, indent, "SetVariable %r value=%r" % (self.vnameexp, self.value)

class Condition(object):
    """
    An abstract "condition", either ifeq or ifdef, perhaps negated. Subclasses must implement:

    def evaluate(self, makefile)
    """

class EqCondition(Condition):
    expected = True

    def __init__(self, exp1, exp2):
        assert isinstance(exp1, data.Expansion)
        assert isinstance(exp2, data.Expansion)

        self.exp1 = exp1
        self.exp2 = exp2

    def evaluate(self, makefile):
        r1 = self.exp1.resolve(makefile, makefile.variables)
        r2 = self.exp2.resolve(makefile, makefile.variables)
        return (r1 == r2) == self.expected

    def __str__(self):
        return "ifeq (expected=%s) %r %r" % (self.expected, self.exp1, self.exp2)

class IfdefCondition(Condition):
    expected = True

    def __init__(self, exp):
        assert isinstance(exp, data.Expansion)
        self.exp = exp

    def evaluate(self, makefile):
        vname = self.exp.resolve(makefile, makefile.variables)
        flavor, source, value = makefile.variables.get(vname, expand=False)

        _log.debug("ifdef at %s: vname: %r value is %r" % (self.exp.loc, vname, value))

        if value is None:
            return not self.expected

        return (len(value) > 0) == self.expected

    def __str__(self):
        return "ifdef (expected=%s) %r" % (self.expected, self.exp)

class ElseCondition(Condition):
    def evaluate(self, makefile):
        return True

    def __str__(self):
        return "else"

class ConditionBlock(Statement):
    """
    A list of conditions: each condition has an associated list of statements.
    """
    def __init__(self, loc, condition):
        self.loc = loc
        self._groups = []
        self.addcondition(loc, condition)

    def getloc(self):
        return self._groups[0][0].loc

    def addcondition(self, loc, condition):
        assert isinstance(condition, Condition)

        if len(self._groups) and isinstance(self._groups[-1][0], ElseCondition):
            raise parser.SyntaxError("Multiple else conditions for block starting at %s" % self.loc, loc)

        self._groups.append((condition, StatementList()))

    def append(self, statement):
        self._groups[-1][1].append(statement)

    def execute(self, makefile, context):
        i = 0
        for c, statements in self._groups:
            if c.evaluate(makefile):
                _log.debug("Condition at %s met by clause #%i" % (self.loc, i))
                statements.execute(makefile, context)
                return

            i += 1

    def dump(self, fd, indent):
        print >>fd, indent, "ConditionBlock"

        indent1 = indent + ' '
        indent2 = indent + '  '
        for c, statements in self._groups:
            print >>fd, indent1, "Condition %s" % (c,)
            for s in statements:
                s.dump(fd, indent2)
        print >>fd, indent, "~ConditionBlock"

class Include(Statement):
    def __init__(self, exp, required):
        assert isinstance(exp, data.Expansion)
        self.exp = exp
        self.required = required

    def execute(self, makefile, context):
        files = data.splitwords(self.exp.resolve(makefile, makefile.variables))
        for f in files:
            makefile.include(f, self.required, loc=self.exp.loc)

    def dump(self, fd, indent):
        print >>fd, indent, "Include %r" % (self.exp,)

class VPathDirective(Statement):
    def __init__(self, exp):
        assert isinstance(exp, data.Expansion)
        self.exp = exp

    def execute(self, makefile, context):
        words = data.splitwords(self.exp.resolve(makefile, makefile.variables))
        if len(words) == 0:
            makefile.clearallvpaths()
        else:
            pattern = data.Pattern(words[0])
            mpaths = words[1:]

            if len(mpaths) == 0:
                makefile.clearvpath(pattern)
            else:
                dirs = []
                for mpath in mpaths:
                    dirs.extend((dir for dir in mpath.split(':')
                                 if dir != ''))
                if len(dirs):
                    makefile.addvpath(pattern, dirs)

    def dump(self, fd, indent):
        print >>fd, indent, "VPath %r" % (self.exp,)

class ExportDirective(Statement):
    def __init__(self, exp, single):
        assert isinstance(exp, data.Expansion)
        self.exp = exp
        self.single = single

    def execute(self, makefile, context):
        if self.single:
            vlist = [self.exp.resolve(makefile, makefile.variables)]
        else:
            vlist = data.splitwords(self.exp.resolve(makefile, makefile.variables))
            if not len(vlist):
                raise data.DataError("Exporting all variables is not supported", self.exp.loc)

        for v in vlist:
            makefile.exportedvars.add(v)

    def dump(self, fd, indent):
        print >>fd, indent, "Export (single=%s) %r" % (self.single, self.exp)

class EmptyDirective(Statement):
    def __init__(self, exp):
        assert isinstance(exp, data.Expansion)
        self.exp = exp

    def execute(self, makefile, context):
        v = self.exp.resolve(makefile, makefile.variables)
        if v.strip() != '':
            raise data.DataError("Line expands to non-empty value", self.exp.loc)

    def dump(self, fd, indent):
        print >>fd, indent, "EmptyDirective: %r" % self.exp

class StatementList(list):
    def append(self, statement):
        assert isinstance(statement, Statement)
        list.append(self, statement)

    def execute(self, makefile, context=None):
        if context is None:
            context = util.makeobject('currule')

        for s in self:
            s.execute(makefile, context)

    def __str__(self):
        fd = StringIO()
        print >>fd, "StatementList"
        for s in self:
            s.dump(fd, ' ')
        print >>fd, "~StatementList"
        return fd.getvalue()
"""Where the genome's tests find a Regista checkout (spec .regista/specs/genome-standalone.json, SEP-6).

The genome folder can be copied or cloned anywhere, with no Regista checkout around it. The tests that need Regista's
command line or its files (the Regista adapter's tests) find a checkout the way evolve.py does, minus evolve.json:

1. the environment variable GENOME_REGISTA_CHECKOUT (an absolute path), when it is set and not empty;
2. else the folder holding genome/, when it has scripts/regista.py (the genome inside a Regista checkout);
3. else none: those tests are skipped with SKIP_REASON, and every other test runs.

The checkout is reached only by path and by running its command line as an outside program: nothing is imported from
it. genome/tests/e2e/harness.py holds a twin of this helper, because the end-to-end folder is no package.

Use: `from regista_location import REGISTA_CHECKOUT, SKIP_REASON, requires_regista` (genome/tests on sys.path), then
decorate a test class or method with @requires_regista, or call skip_without_regista(test) inside setUpClass."""
import os
import unittest

GENOME = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # the genome folder
SKIP_REASON = "no Regista checkout: set GENOME_REGISTA_CHECKOUT"
NOT_HOLDING_REASON = "the genome folder isn't inside the Regista checkout, so its layout there can't be checked"


def regista_checkout(environ=None):
    """The Regista checkout the tests use (an absolute path), or None when none is found."""
    environ = os.environ if environ is None else environ
    named = (environ.get("GENOME_REGISTA_CHECKOUT") or "").strip()
    if named:
        return os.path.abspath(named)
    around = os.path.dirname(GENOME)
    if os.path.isfile(os.path.join(around, "scripts", "regista.py")):
        return around
    return None


REGISTA_CHECKOUT = regista_checkout()
# Regista's command line in that checkout (None without one).
REGISTA_TOOL = os.path.join(REGISTA_CHECKOUT, "scripts", "regista.py") if REGISTA_CHECKOUT else None
# Whether this genome folder is the one inside that checkout (the repository's own layout can be checked).
HOLDS_GENOME = bool(REGISTA_CHECKOUT) and os.path.realpath(os.path.dirname(GENOME)) == \
    os.path.realpath(REGISTA_CHECKOUT)

requires_regista = unittest.skipIf(REGISTA_CHECKOUT is None, SKIP_REASON)


def requires_genome_inside_regista(test):
    """Skip a test about the genome folder's place in the Regista repository unless it sits there."""
    if REGISTA_CHECKOUT is None:
        return unittest.skip(SKIP_REASON)(test)
    return unittest.skipUnless(HOLDS_GENOME, NOT_HOLDING_REASON)(test)


def checkout_path(*parts):
    """A path inside the Regista checkout; the dummy folder name when there is none (the tests using it are skipped)."""
    return os.path.join(REGISTA_CHECKOUT or os.path.join(os.path.dirname(GENOME), "no-regista-checkout"), *parts)

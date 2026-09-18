"""Find names that are used but never defined, imported or built in.

Written because an editor reported "ProviderError is not defined" in a module that raised it
without importing it, and there was no linter in the environment to catch that class of bug.
This walks the AST of every file under py/ and reports names that are referenced but cannot
be resolved from the module's own definitions, its imports, its enclosing scopes, or the
builtins.

It is deliberately conservative: it understands module-level and nested function scopes,
comprehensions, global/nonlocal, star imports (which it treats as "anything may be defined",
so a module using them is skipped for that name), and it ignores attribute accesses. Anything
it reports is worth a look; anything it does not report may still be wrong.
"""
import ast
import builtins
import os
import sys

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "self", "cls", "True",
                                 "False", "None", "__class__", "__spec__", "__package__"}


def target_names(node):
    """Names bound by an assignment target, for-loop target, with-item or similar."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
    return out


class ScopeCheck(ast.NodeVisitor):
    def __init__(self, module_names, path):
        self.module_names = module_names
        self.path = path
        self.problems = []
        self.star_import = False

    # --- scope handling -------------------------------------------------
    def visit_Import(self, node):
        for a in node.names:
            self.module_names.add((a.asname or a.name).split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                self.star_import = True
            else:
                self.module_names.add(a.asname or a.name)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id not in self.module_names:
            if node.id not in BUILTINS:
                self.problems.append((node.lineno, node.id))
        self.generic_visit(node)


def scan_file(path):
    src = open(path, "r", encoding="utf-8", errors="replace").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [("syntax error: {}".format(e), 0)], False

    # pass 1: every name bound at module level (defs, classes, assignments, imports)
    module_names = set()
    star = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    star = True
                else:
                    module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            module_names |= target_names(node)
        elif isinstance(node, (ast.For, ast.While, ast.If, ast.Try, ast.With)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    module_names |= target_names(sub)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    module_names.add(sub.name)

    # pass 2: local scopes
    checker = ScopeCheck(set(module_names), path)
    checker.star_import = star
    checker.visit(tree)

    # filter: a name bound ANYWHERE in the file counts as defined for this file's purposes.
    # Bound includes the places a naive walk misses: except ... as e, comprehension and
    # for-loop targets, with ... as x, lambda parameters, del targets and our own names.
    all_assigned = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            all_assigned.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            all_assigned.add(n.name)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                all_assigned.update(x.arg for x in a.args + a.kwonlyargs + a.posonlyargs)
                if a.vararg:
                    all_assigned.add(a.vararg.arg)
                if a.kwarg:
                    all_assigned.add(a.kwarg.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            all_assigned.add(n.name)                      # except ValueError as e
        elif isinstance(n, ast.comprehension):
            all_assigned |= target_names(n.target)        # [x for x in ...]
        elif isinstance(n, ast.withitem):
            if n.optional_vars is not None:
                all_assigned |= target_names(n.optional_vars)
        elif isinstance(n, ast.Lambda):
            all_assigned.update(x.arg for x in n.args.args + n.args.kwonlyargs)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            all_assigned.update(n.names)
    seen = set()
    problems = []
    for lineno, name in checker.problems:
        if name in all_assigned or name in module_names:
            continue
        if (lineno, name) in seen:
            continue
        seen.add((lineno, name))
        problems.append((name, lineno))
    return problems, star


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join("py")
    total = 0
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", "node_modules")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            files += 1
            path = os.path.join(dirpath, fn)
            problems, star = scan_file(path)
            for name, lineno in problems:
                total += 1
                print("  {}:{}: '{}' is used but never defined or imported{}".format(
                    path.replace("\\", "/"), lineno, name,
                    "  (file uses a star import, may be false)" if star else ""))
    print("  scanned {} files, {} unresolved name(s)".format(files, total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

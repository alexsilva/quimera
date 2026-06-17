"""Ferramentas git estruturadas para o runtime do Quimera."""
from __future__ import annotations

import re

from quimera import process_factory as subprocess

from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from .base import ToolBase, ValidatableTool

_SAFE_REF_RE = re.compile(r'^[a-zA-Z0-9._\-/]+$')
_SAFE_REMOTE_RE = re.compile(r'^[a-zA-Z0-9._\-]+$')


class GitTool(ToolBase, tool_prefix="git"):
    """Operações git com output estruturado."""

    # ------------------------------------------------------------------
    # Operações de leitura (sem approval)
    # ------------------------------------------------------------------

    def git_status(self, call: ToolCall) -> ToolResult:
        """Retorna o status do repositório git de forma estruturada."""
        rc, stdout, stderr = self._run_git(["status", "--porcelain=v1", "-b"])
        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=stderr.strip() or "git status failed")

        branch, ahead, behind, staged, unstaged, untracked = self._parse_status(stdout)
        is_clean = not staged and not unstaged and not untracked

        data = {
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "ahead": ahead,
            "behind": behind,
            "clean": is_clean,
        }
        content = self._format_status(branch, ahead, behind, staged, unstaged, untracked, is_clean)
        return ToolResult(ok=True, tool_name=call.name, content=content, data=data)

    def git_log(self, call: ToolCall) -> ToolResult:
        """Retorna commits recentes de forma estruturada."""
        max_count = max(1, min(200, int(call.arguments.get("max_count", 10))))
        branch = str(call.arguments.get("branch") or "").strip()

        fmt = "%H%x00%h%x00%an%x00%ae%x00%ad%x00%s"
        args = ["log", f"--format={fmt}", "--date=iso-strict", f"-n{max_count}"]
        if branch:
            args.append(branch)

        rc, stdout, stderr = self._run_git(args)
        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=stderr.strip() or "git log failed")

        commits = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x00")
            if len(parts) >= 6:
                commits.append({
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "author": parts[2],
                    "author_email": parts[3],
                    "date": parts[4],
                    "message": parts[5],
                })

        data = {"commits": commits}
        if commits:
            content = "\n".join(
                f"{c['short_hash']} {c['date'][:10]} {c['author']}: {c['message']}"
                for c in commits
            )
        else:
            content = "(no commits)"
        return ToolResult(ok=True, tool_name=call.name, content=content, data=data)

    def git_diff(self, call: ToolCall) -> ToolResult:
        """Retorna o diff do repositório (working tree, staged ou entre refs)."""
        staged = bool(call.arguments.get("staged", False))
        path = str(call.arguments.get("path") or "").strip()
        ref1 = str(call.arguments.get("ref1") or "").strip()
        ref2 = str(call.arguments.get("ref2") or "").strip()

        base_args = ["diff"]
        if staged:
            base_args.append("--staged")
        if ref1 and ref2:
            base_args.extend([ref1, ref2])
        elif ref1:
            base_args.append(ref1)

        diff_args = list(base_args)
        if path:
            diff_args.extend(["--", path])

        stat_args = list(base_args) + ["--stat"]
        if path:
            stat_args.extend(["--", path])

        rc, diff_out, diff_err = self._run_git(diff_args)
        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=diff_err.strip() or "git diff failed")

        _, stat_out, _ = self._run_git(stat_args)

        truncated = False
        visible_diff = diff_out
        if len(diff_out) > self.config.max_output_chars:
            visible_diff = diff_out[: self.config.max_output_chars]
            truncated = True

        data = {
            "diff": visible_diff,
            "stat": stat_out.strip(),
            "staged": staged,
        }
        if path:
            data["path"] = path

        content = stat_out.strip() or "(no changes)"
        if visible_diff:
            content += "\n\n" + visible_diff

        return ToolResult(ok=True, tool_name=call.name, content=content, truncated=truncated, data=data)

    def git_branch(self, call: ToolCall) -> ToolResult:
        """Lista branches locais (e opcionalmente remotas)."""
        all_branches = bool(call.arguments.get("all", False))
        remote_only = bool(call.arguments.get("remote", False))

        args = ["branch", "--format=%(refname:short)|%(upstream:short)|%(HEAD)"]
        if all_branches:
            args.append("--all")
        elif remote_only:
            args.append("--remote")

        rc, stdout, stderr = self._run_git(args)
        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=stderr.strip() or "git branch failed")

        branches = []
        current = ""
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            name = parts[0] if parts else ""
            upstream = parts[1] if len(parts) > 1 else ""
            is_current = parts[2].strip() == "*" if len(parts) > 2 else False
            if is_current:
                current = name
            branches.append({"name": name, "upstream": upstream, "current": is_current})

        data = {"branches": branches, "current": current}
        lines = []
        for b in branches:
            prefix = "* " if b["current"] else "  "
            suffix = f" → {b['upstream']}" if b["upstream"] else ""
            lines.append(f"{prefix}{b['name']}{suffix}")
        content = "\n".join(lines) or "(no branches)"
        return ToolResult(ok=True, tool_name=call.name, content=content, data=data)

    def git_fetch(self, call: ToolCall) -> ToolResult:
        """Faz fetch do remote (atualiza refs remotas sem alterar working tree)."""
        remote = str(call.arguments.get("remote") or "").strip()
        prune = bool(call.arguments.get("prune", False))

        args = ["fetch"]
        if prune:
            args.append("--prune")
        if remote:
            args.append(remote)

        rc, stdout, stderr = self._run_git(args)
        output = (stdout + "\n" + stderr).strip()

        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=output or "git fetch failed")

        data = {"ok": True, "remote": remote or "origin", "output": output}
        return ToolResult(ok=True, tool_name=call.name, content=output or "fetch ok", data=data)

    # ------------------------------------------------------------------
    # Operações de mutação (require approval)
    # ------------------------------------------------------------------

    def git_add(self, call: ToolCall) -> ToolResult:
        """Adiciona arquivos ao índice (staging area)."""
        raw_paths = call.arguments.get("paths")
        if raw_paths is None:
            paths = ["."]
        elif isinstance(raw_paths, str):
            paths = [raw_paths]
        else:
            paths = list(raw_paths)

        rc, _, stderr = self._run_git(["add", "--"] + paths)
        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=stderr.strip() or "git add failed")

        _, status_out, _ = self._run_git(["status", "--porcelain=v1"])
        staged_files = [
            line[3:].strip()
            for line in status_out.splitlines()
            if line and line[0] not in (" ", "?")
        ]

        data = {"ok": True, "paths": paths, "staged": staged_files}
        if staged_files:
            content = f"staged {len(staged_files)} file(s): " + ", ".join(staged_files)
        else:
            content = "nothing staged"
        return ToolResult(ok=True, tool_name=call.name, content=content, data=data)

    def git_commit(self, call: ToolCall) -> ToolResult:
        """Cria um commit com os arquivos staged."""
        message = str(call.arguments.get("message", "")).strip()
        amend = bool(call.arguments.get("amend", False))

        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")

        rc, stdout, stderr = self._run_git(args)
        output = (stdout + "\n" + stderr).strip()

        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=output or "git commit failed")

        _, hash_out, _ = self._run_git(["rev-parse", "HEAD"])
        commit_hash = hash_out.strip()
        short_hash = commit_hash[:7] if commit_hash else ""

        data = {"ok": True, "commit": commit_hash, "short_hash": short_hash, "message": message}
        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=f"commit {short_hash}: {message}",
            data=data,
        )

    def git_checkout(self, call: ToolCall) -> ToolResult:
        """Muda de branch ou cria uma nova (checkout [-b] <branch>)."""
        branch = str(call.arguments.get("branch", "")).strip()
        create = bool(call.arguments.get("create", False))

        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(branch)

        rc, stdout, stderr = self._run_git(args)
        output = (stdout + "\n" + stderr).strip()

        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=output or f"git checkout {branch} failed")

        data = {"ok": True, "branch": branch, "created": create, "output": output}
        action = "created and switched to" if create else "switched to"
        return ToolResult(ok=True, tool_name=call.name, content=f"{action} branch '{branch}'", data=data)

    def git_push(self, call: ToolCall) -> ToolResult:
        """Faz push para um remote. Force-push é bloqueado pela policy."""
        remote = str(call.arguments.get("remote") or "origin").strip()
        branch = str(call.arguments.get("branch") or "").strip()
        set_upstream = bool(call.arguments.get("set_upstream", False))

        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.append(remote)
        if branch:
            args.append(branch)

        rc, stdout, stderr = self._run_git(args)
        output = (stdout + "\n" + stderr).strip()

        if rc != 0:
            return ToolResult(ok=False, tool_name=call.name, error=output or f"git push to {remote} failed")

        data = {"ok": True, "remote": remote, "branch": branch, "output": output}
        return ToolResult(ok=True, tool_name=call.name, content=output or f"pushed to {remote}", data=data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_git(self, args: list[str]) -> tuple[int, str, str]:
        """Executa um comando git no workspace root."""
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(self.config.workspace_root),
            capture_output=True,
            text=True,
            timeout=self.config.command_timeout_seconds,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    @staticmethod
    def _parse_status(stdout: str) -> tuple[str, int, int, list, list, list]:
        """Analisa a saída de `git status --porcelain=v1 -b`."""
        lines = stdout.splitlines()
        branch = ""
        ahead = 0
        behind = 0
        staged: list[dict] = []
        unstaged: list[dict] = []
        untracked: list[str] = []

        for line in lines:
            if line.startswith("## "):
                info = line[3:]
                parts = info.split("...")
                branch = parts[0].strip()
                ahead_m = re.search(r"ahead (\d+)", info)
                behind_m = re.search(r"behind (\d+)", info)
                if ahead_m:
                    ahead = int(ahead_m.group(1))
                if behind_m:
                    behind = int(behind_m.group(1))
            elif line.startswith("??"):
                untracked.append(line[3:].strip())
            elif len(line) >= 2:
                x = line[0]
                y = line[1]
                path = line[3:].strip()
                if x not in (" ", "?"):
                    staged.append({"status": x, "path": path})
                if y not in (" ", "?"):
                    unstaged.append({"status": y, "path": path})

        return branch, ahead, behind, staged, unstaged, untracked

    @staticmethod
    def _format_status(
        branch: str,
        ahead: int,
        behind: int,
        staged: list,
        unstaged: list,
        untracked: list,
        is_clean: bool,
    ) -> str:
        """Formata o status para exibição textual."""
        parts = [f"branch: {branch}"]
        if ahead or behind:
            parts.append(f"ahead: {ahead}, behind: {behind}")
        if staged:
            files = ", ".join(f"{s['status']} {s['path']}" for s in staged)
            parts.append(f"staged ({len(staged)}): {files}")
        if unstaged:
            files = ", ".join(f"{s['status']} {s['path']}" for s in unstaged)
            parts.append(f"unstaged ({len(unstaged)}): {files}")
        if untracked:
            parts.append(f"untracked ({len(untracked)}): " + ", ".join(untracked))
        if is_clean:
            parts.append("clean")
        return "\n".join(parts)


class GitToolValidator(ValidatableTool):
    """Validação de policy para as ferramentas git."""

    def _validate_git_status(self, call: ToolCall) -> None:
        """git_status não requer argumentos."""

    def _validate_git_log(self, call: ToolCall) -> None:
        """Valida git_log: max_count e branch opcionais."""
        max_count = call.arguments.get("max_count")
        if max_count is not None:
            try:
                v = int(max_count)
            except (TypeError, ValueError) as exc:
                raise ToolPolicyError("git_log.max_count deve ser inteiro positivo") from exc
            if v <= 0:
                raise ToolPolicyError("git_log.max_count deve ser inteiro positivo")
        branch = call.arguments.get("branch")
        if branch is not None:
            self._validate_git_ref(str(branch), field="branch")

    def _validate_git_diff(self, call: ToolCall) -> None:
        """Valida git_diff: path restrito ao workspace, refs seguros."""
        path = call.arguments.get("path")
        if path:
            self._resolve_workspace_path(str(path))
        ref1 = call.arguments.get("ref1")
        ref2 = call.arguments.get("ref2")
        if ref1:
            self._validate_git_ref(str(ref1), field="ref1")
        if ref2:
            self._validate_git_ref(str(ref2), field="ref2")

    def _validate_git_branch(self, call: ToolCall) -> None:
        """git_branch não requer argumentos obrigatórios."""

    def _validate_git_fetch(self, call: ToolCall) -> None:
        """Valida git_fetch: remote deve ser nome seguro."""
        remote = call.arguments.get("remote")
        if remote:
            self._validate_git_remote(str(remote))

    def _validate_git_add(self, call: ToolCall) -> None:
        """Valida git_add: todos os paths devem estar dentro do workspace."""
        raw = call.arguments.get("paths")
        if raw is None:
            return
        if isinstance(raw, str):
            paths = [raw]
        elif isinstance(raw, list):
            paths = [str(p) for p in raw]
        else:
            raise ToolPolicyError("git_add.paths deve ser string ou lista de strings")
        for p in paths:
            if p.strip() in (".", ""):
                continue
            self._resolve_workspace_path(p)

    def _validate_git_commit(self, call: ToolCall) -> None:
        """Valida git_commit: message é obrigatória e não pode ser vazia."""
        message = call.arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolPolicyError("git_commit requer 'message' não vazia")
        if len(message) > 10_000:
            raise ToolPolicyError("git_commit.message excede o limite de 10000 caracteres")

    def _validate_git_checkout(self, call: ToolCall) -> None:
        """Valida git_checkout: branch deve ser nome seguro."""
        branch = call.arguments.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise ToolPolicyError("git_checkout requer 'branch' não vazio")
        self._validate_git_ref(branch.strip(), field="branch")

    def _validate_git_push(self, call: ToolCall) -> None:
        """Valida git_push: remote/branch seguros, force-push bloqueado."""
        remote = call.arguments.get("remote") or "origin"
        self._validate_git_remote(str(remote))
        branch = call.arguments.get("branch")
        if branch:
            self._validate_git_ref(str(branch), field="branch")

    @staticmethod
    def _validate_git_ref(value: str, *, field: str) -> None:
        """Valida que um nome de ref git não contém metacaracteres de shell."""
        if not value or not _SAFE_REF_RE.match(value):
            raise ToolPolicyError(
                f"git: {field} inválido '{value}'; use apenas letras, números, '.', '_', '-' ou '/'"
            )

    @staticmethod
    def _validate_git_remote(value: str) -> None:
        """Valida que um nome de remote git é seguro."""
        if not value or not _SAFE_REMOTE_RE.match(value):
            raise ToolPolicyError(
                f"git: remote inválido '{value}'; use apenas letras, números, '.', '_' ou '-'"
            )


def register(registry, policy, config) -> None:
    """Registra todas as tools git no registry e a validação na policy."""
    git_tool = GitTool(config)
    git_validator = GitToolValidator(config)
    tool_names = [name for name in dir(GitTool) if name.startswith("git_")]
    for name in tool_names:
        registry.register(name, getattr(git_tool, name))
    policy.register_tool_validator(tool_names, git_validator)

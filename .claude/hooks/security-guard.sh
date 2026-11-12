#!/bin/bash
# managed-by: kew
# security-guard.sh — Block destructive operations before they execute.
# Runs as a PreToolUse hook, BEFORE the permission system.
# Works even with --dangerously-skip-permissions.
#
# Deliberate non-goals: comment and quote masking, heredoc body masking,
# backslash-newline continuation, shell expansions, eval payloads, interpreter
# payloads, and sh -c payload inspection beyond the prefix list below. Reserved
# word heads, xargs -a, and wrappers outside the documented list are also out of
# scope, as are: subshell and group forms without an inner space ((cmd), {cmd;}),
# options on the listed wrappers (command -p, timeout -k 1 5, nice --adjustment),
# NAME=value assignment prefixes before a command, and /proc environ reads via
# readers outside the listed set, via relative paths other than a bare
# 'environ' after cd, or behind an fd prefix wider than one digit (10< ...).
# The pipe rule covers a fixed interpreter list (bash sh zsh
# dash ksh python python3 perl ruby node); other interpreters, a bare script
# name without / or ., an unlisted option whose argument looks like a path, and
# a quoted program path that contains whitespace (word-splitting precedes
# quote handling, so it is denied) are outside it. Those shapes require a shell parser,
# which this denylist is not.

INPUT=$(cat)

# --- Fail CLOSED when jq is unavailable ---
# jq parses the tool payload; without it the guard cannot inspect anything and
# would silently allow every tool call through (layer-2 becomes a no-op). Refuse
# instead: emit a deny decision AND exit nonzero so the tool call is blocked.
if ! command -v jq >/dev/null 2>&1; then
    cat << 'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: jq is required to inspect tool calls but is not installed (failing closed)."}}
EOF
    exit 1
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# --- Fail CLOSED when the tool name can't be parsed ---
# An empty tool_name means the payload was malformed, truncated, or tampered —
# jq yielded nothing. The guard then cannot tell whether this is a Bash call
# carrying a destructive command, so it must DENY rather than fall through to
# the `[ "$TOOL_NAME" != "Bash" ] && exit 0` allow path below (which would let
# an unparseable payload skip every check — the layer-2 no-op this hook exists
# to prevent). Emit a deny decision AND exit nonzero so the tool call is blocked.
if [ -z "$TOOL_NAME" ]; then
    cat << 'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: tool_name missing or unparseable from the tool payload (failing closed)."}}
EOF
    exit 1
fi

# --- Write/Edit protection: block modifications to .claude/ ---
#
# APPLIES TO DISPATCHED AGENTS ONLY, for the same reason the ref-mutation rule
# does: `kew setup` installs this guard at the PROJECT root, so it loads for the
# human's own interactive session too, and AGENT_ASSIGNED_WORKTREE is the only
# signal telling the two apart (set by orchestrator.run_agent, the single agent
# spawn point, and by nothing else).
#
# Without this, an operator cannot edit their own hooks, settings, or skills
# from their own editor — while the message told them the rule was about
# "hooks and settings", which is a narrower claim than the rule enforced.
#
# The agent-side protection is unchanged and deliberately covers ALL of
# `.claude/`, not just hooks and settings: `.claude/skills/**` is instructions
# that later sessions load and auto-invoke by description, so an agent able to
# write a skill can plant durable instructions for the sessions after it. That
# is a persistence and injection surface, not a config file, which is why this
# rule stays broad for agents even though the operator needs the opposite.
if [ -n "${AGENT_ASSIGNED_WORKTREE:-}" ] &&
    { [ "$TOOL_NAME" = "Write" ] || [ "$TOOL_NAME" = "Edit" ]; }; then
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
    if [ -n "$FILE_PATH" ]; then
        # Canonicalize path to prevent relative-path and symlink bypasses
        if command -v realpath >/dev/null 2>&1; then
            CANON_PATH=$(realpath -m -- "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")
        else
            CANON_PATH=$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")
        fi
        # Allow Claude Code auto-memory (project memory notes) — nothing else
        # under .claude/ is writable (hooks and settings stay protected).
        case "$CANON_PATH" in
            "$HOME"/.claude/projects/*/memory/*)
                exit 0
                ;;
        esac
        # Block writes inside any .claude directory (absolute or relative)
        case "$CANON_PATH" in
            */.claude/*|*/.claude|.claude/*|.claude)
                cat << EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: writes to .claude/ are not allowed (protects hooks and settings)."}}
EOF
                exit 0
                ;;
        esac
    fi
    exit 0
fi

# --- Bash command inspection ---
[ "$TOOL_NAME" != "Bash" ] && exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# --- Prefix patterns: block if a command segment STARTS with these ---
PREFIX_PATTERNS=(
    "git push --force"
    "git push -f"
    "git push origin +main"
    "git push origin +master"
    "git branch -D main"
    "git branch -D master"
    "git branch -d main"
    "git branch -d master"
    "git checkout --orphan"
    "git reset --hard"
    "git clean -fd"
    "sudo "
    "chmod 777"
    "eval "
    "base64 -d"
    "base64 --decode"
    "printenv"
    "echo \$ANTHROPIC"
    "echo \$GITHUB_TOKEN"
    "echo \$GH_TOKEN"
    "sh -c "
    "bash -c "
# EXTRA_PREFIX_PATTERNS
)

# --- Exact patterns: block the exact command but not subpaths ---
EXACT_PATTERNS=(
    "rm -rf /"
    "rm -rf /*"
    "rm -rf ."
    "> /dev/sda"
    # `env` alone dumps the whole environment; `env VAR=val cmd` is also caught
    # ("env "* below). Exact form covers the bare dump the prefix list missed.
    "env"
# EXTRA_EXACT_PATTERNS
)

# Check for pipe-to-shell patterns in the full command (S3, S4)
case "$COMMAND" in
    *"| bash"*|*"| sh"*|*"| zsh"*)
        cat << EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: pipe-to-shell is not permitted."}}
EOF
        exit 0
        ;;
esac

# Check for file exfiltration via curl/wget (N2) — flags can appear anywhere
case "$COMMAND" in
    *"curl "*"-d @"*|*"curl "*"--data @"*|*"curl "*"--data-binary @"*|*"curl "*"--data-raw @"*|*"curl "*"--upload-file "*|*"curl "*"-F "*"@"*|*"curl "*"--form "*"@"*|*"wget "*"--post-file"*)
        cat << EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: file upload via curl/wget is not permitted (data exfiltration risk)."}}
EOF
        exit 0
        ;;
esac

strip_outer_quotes() {
    local value="$1"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

resolve_directory() {
    local requested="$1"
    local base_dir="$2"
    requested=$(strip_outer_quotes "$requested")
    case "$requested" in
        --\ *) requested="${requested#-- }"; requested=$(strip_outer_quotes "$requested") ;;
    esac
    case "$requested" in
        /*) ;;
        *)
            # Without a known base, a relative path cannot be resolved at all —
            # joining it onto "" would silently resolve against the filesystem
            # root.  Report failure so the caller fails closed.
            [ -z "$base_dir" ] && return 1
            requested="$base_dir/$requested"
            ;;
    esac
    (cd -- "$requested" 2>/dev/null && pwd -P)
}

# `cd` takes option flags (-L, -P, -e, -@, and `--`) before the directory.
# Strip them so the directory argument is what gets resolved.  An option shape
# this guard does not model exits nonzero, and the caller then treats the
# resulting checkout as unknown rather than assuming the cwd never moved.
strip_cd_options() {
    local args="$1"
    local token flags
    while [ -n "$args" ]; do
        token="${args%% *}"
        case "$token" in
            --)
                [ "$token" = "$args" ] && return 1
                printf '%s' "${args#-- }"
                return 0
                ;;
            -*)
                [ "$token" = "$args" ] && return 1
                flags="${token#-}"
                [ -n "$(printf '%s' "$flags" | tr -d 'LPe@')" ] && return 1
                args="${args#"$token" }"
                ;;
            *)
                printf '%s' "$args"
                return 0
                ;;
        esac
    done
    return 1
}

# A hook receives the command before the shell executes it.  Track the cwd each
# `cd` segment establishes so the checks below inspect the checkout the command
# will actually mutate, rather than the hook's cwd.  A `cd` whose target cannot
# be resolved (an option form we do not model, a variable, a missing directory)
# empties COMMAND_CWD, which denies every ref mutation that follows it.
COMMAND_CWD=$(pwd -P)
PROC_ENV_CWD=""
track_cd_segment() {
    local segment="$1"
    local target proc_target
    while :; do
        case "$segment" in
            \(*) segment="${segment#\(}" ;;
            [[:space:]]*) segment="${segment#"${segment%%[![:space:]]*}"}" ;;
            *) break ;;
        esac
    done
    case "$segment" in
        cd\ *) ;;
        cd) COMMAND_CWD=""; return 0 ;;
        *) return 0 ;;
    esac
    if target=$(strip_cd_options "${segment#cd }"); then
        proc_target=$(strip_outer_quotes "$target")
        COMMAND_CWD=$(resolve_directory "$target" "$COMMAND_CWD" || true)
    else
        COMMAND_CWD=""
    fi
    if [[ "$proc_target" =~ ^/proc/([0-9]+|self|thread-self)(/|$) ]] ||
        [[ "$COMMAND_CWD" =~ ^/proc/([0-9]+|self|thread-self)(/|$) ]]; then
        PROC_ENV_CWD=1
    else
        PROC_ENV_CWD=""
    fi
}

check_segment() {
    local trimmed="$1"
    local is_pipe_target="${2:-}"
    [ -z "$trimmed" ] && return 1

    # Detect process substitution — <(...) input or >(...) output forms (F3)
    case "$trimmed" in
        *'<('*|*'>('*)
            echo "process substitution"
            return 0
            ;;
    esac

    # Detect subshell injection ($(...) or backtick substitution)
    case "$trimmed" in
        *'$('*|*\`*)
            echo "subshell"
            return 0
            ;;
    esac

    # Collapse internal whitespace for comparison so "git push  --force" (F2)
    # is caught by the "git push --force" prefix pattern.  Keep $trimmed for
    # deny messages; use $normalized only for the pattern match below.
    local normalized
    normalized=$(printf '%s' "$trimmed" | tr -s '[:space:]' ' ')

    # Peel only the documented command wrappers. This is deliberately a
    # whitespace-word check, not a shell parser.
    local command_text="$normalized" word name rest env_word redirect_tail leading_redirects=""
    while :; do
        command_text="${command_text#"${command_text%%[![:space:]]*}"}"
        case "$command_text" in \\*) command_text="${command_text#\\}" ;; "( "*) command_text="${command_text#( }"; command_text="${command_text% )}"; command_text="${command_text%)}" ;; "{ "*) command_text="${command_text#{ }" ;; esac
        # A segment can begin with its input redirect and carry no command word
        # at all (`< file`, `0<file`, and the same shapes once `exec` is
        # peeled).  Split the operator off its operand so the word loop reads
        # the target as a word instead of reducing `</proc/self/environ` to the
        # basename `environ`.  Only a single-digit fd prefix is modelled.
        case "$command_text" in '<'[!\<\(]*|[0-9]'<'[!\<\(]*) redirect_tail="${command_text#*<}"; command_text="${command_text%%<*}< ${redirect_tail# }" ;; esac
        # A redirect placed BEFORE the command word (`</dev/null cp a .claude/x`,
        # `2>/dev/null printenv`, `0<f bash -c ls`) must not become the command
        # word, or every first-word rule below falls through.  Move the
        # operator and its operand into $leading_redirects (fed to the /proc
        # scan only — never to the command's argument list, so a piped
        # interpreter cannot borrow them as a "program") and keep peeling.
        # The redirect operators modelled before the command word: `<`, `>`,
        # `>>`, `>|`, `&>`, `&>>`, `<<<`, `<<`, each with an optional
        # single-digit fd.  An operator that stands alone takes the NEXT
        # word as its operand (`2> /dev/null printenv`); a glued token is
        # moved whole (`2>/dev/null printenv`, `<<<x cp …`).  Not modelled
        # (documented non-goals, tracked on #1686): the spaced forms of
        # `N>|`, `N<<<`, `<>`, `N>&`, `>&`, `<&`, `<<-`, and `{var}<` fd
        # allocation (bash >= 4.1).
        case "$command_text" in
            [0-9]'<'*|'<'*|[0-9]'>'*|'>'*|'&>'*)
                case "$command_text" in '<('*|'>('*) ;; *)
                    case "${command_text%% *}" in
                        [0-9]'<'|'<'|[0-9]'>'|'>'|[0-9]'>>'|'>>'|'>|'|'&>'|'&>>'|'<<<'|[0-9]'<<'|'<<')
                            redirect_tail="${command_text#* }"
                            case "$redirect_tail" in *' '*) leading_redirects="$leading_redirects ${command_text%% *} ${redirect_tail%% *}"; command_text="${redirect_tail#* }"; continue ;; esac ;;
                        *)
                            case "$command_text" in *' '*) leading_redirects="$leading_redirects ${command_text%% *}"; command_text="${command_text#* }"; continue ;; esac ;;
                    esac ;;
                esac ;;
        esac
        word="${command_text%% *}"; rest="${command_text#"$word"}"; rest="${rest# }"
        case "$word" in \"*\") word="${word#\"}"; word="${word%\"}" ;; \'*\') word="${word#\'}"; word="${word%\'}" ;; esac
        name="${word##*/}"
        case "$name" in
            command|builtin|exec|nohup|time) command_text="$rest" ;;
            timeout) command_text="${rest#* }" ;;
            nice) command_text="$rest"; case "$command_text" in -n\ *) command_text="${command_text#* }"; command_text="${command_text#* }" ;; -[0-9]*) command_text="${command_text#* }" ;; esac ;;
            xargs) [ -n "$is_pipe_target" ] && command_text="$rest" || break ;;
            env) command_text="$rest"; while :; do env_word="${command_text%% *}"; case "$env_word" in -*|*=*) command_text="${command_text#"$env_word"}"; command_text="${command_text# }" ;; *) break ;; esac; done; [ -z "$command_text" ] && { echo "env"; return 0; } ;;
            *) command_text="$name${command_text#"${command_text%% *}"}"; break ;;
        esac
    done
    name="${command_text%% *}"
    rest="${command_text#"$name"}"; rest="${rest# }"

    # A force marker is dangerous only after git's push subcommand.
    local git_args git_token
    case "$command_text" in git\ *) git_args="${command_text#git }" ;; *) git_args="" ;; esac
    while :; do
        case "$git_args" in
            -c\ *|-C\ *|--git-dir\ *|--work-tree\ *) git_args="${git_args#* }"; git_args="${git_args#* }" ;;
            --git-dir=*\ *|--work-tree=*\ *|--no-pager\ *|-p\ *) git_args="${git_args#* }" ;;
            *) break ;;
        esac
    done
    case "$git_args" in
        push|push\ *)
            for git_token in ${git_args#push}; do
                case "$git_token" in \"*\") git_token="${git_token#\"}"; git_token="${git_token%\"}" ;; \'*\') git_token="${git_token#\'}"; git_token="${git_token%\'}" ;; esac
                case "$git_token" in --force|--force-with-lease|--force-with-lease=*|--force-if-includes|-f*|-[^-]*f*|+*) echo "git push --force"; return 0 ;; esac
            done
            ;;
    esac

    # Recognize upload options only when curl or wget is this segment's command.
    local upload_mode="" upload_token
    case "$name" in
        curl|wget)
            for upload_token in $rest; do
                case "$upload_token" in \"*\") upload_token="${upload_token#\"}"; upload_token="${upload_token%\"}" ;; \'*\') upload_token="${upload_token#\'}"; upload_token="${upload_token%\'}" ;; esac
                case "$upload_mode:$upload_token" in form:*@?*|data:@?*) echo "file upload via curl/wget"; return 0 ;; esac
                upload_mode=""
                case "$name:$upload_token" in curl:-T*|curl:-[^-]*T*|curl:--upload-file|curl:--upload-file=*|wget:--post-file|wget:--post-file=*|wget:--body-file|wget:--body-file=*) echo "file upload via curl/wget"; return 0 ;; curl:-F|curl:--form) upload_mode=form ;; curl:-F*@?*|curl:--form=*@?*|curl:--data*@?*|curl:-d@?*) echo "file upload via curl/wget"; return 0 ;; curl:-d|curl:--data*|curl:--json) upload_mode=data ;; esac
            done
            ;;
    esac

    # Pipe targets with no program argument execute their stdin as code.
    if [ -n "$is_pipe_target" ]; then
        case "$name" in
            bash|sh|zsh|dash|ksh|python|python3|python3.*|python2*|pypy*|perl|ruby|node)
                # Fail closed: a program is ONLY an explicit per-interpreter
                # program flag or a path-like word (contains / or .). Every
                # other token is an option or an option's argument and is
                # ignored, so a new option spelling (+e, --input-type module,
                # quoted "-e") cannot turn stdin execution into a program.
                # An explicit stdin operand denies regardless.
                local pipe_tok has_program="" reads_stdin="" skip_next="" pipe_name="$name"
                case "$pipe_name" in python3.*|python2*|pypy*) pipe_name=python3 ;; esac
                for pipe_tok in $rest; do
                    if [ -n "$skip_next" ]; then skip_next=""; continue; fi
                    case "$pipe_tok" in
                        \"*\") pipe_tok="${pipe_tok#\"}"; pipe_tok="${pipe_tok%\"}" ;;
                        \'*\') pipe_tok="${pipe_tok#\'}"; pipe_tok="${pipe_tok%\'}" ;;
                        -*\"*|-*\'*) reads_stdin=1; continue ;;
                        *\"*|*\'*) continue ;;
                    esac
                    # A perl/ruby switch cluster carries an inline program only when
                    # every character before the e/E is a switch letter; -nIe is
                    # -n -I e and must not count (round-8 review).
                    if [ "$pipe_name" = perl ] && [[ "$pipe_tok" =~ ^-[lnpaws0-9]*[eE] ]]; then has_program=1; continue; fi
                    if [ "$pipe_name" = ruby ] && [[ "$pipe_tok" =~ ^-[lnpaws0-9]*e ]]; then has_program=1; continue; fi
                    case "$pipe_name:$pipe_tok" in
                        *:-|*:/dev/stdin|*:/dev/fd/0|*:/proc/self/fd/0) reads_stdin=1 ;;
                        bash:-s|sh:-s|zsh:-s|dash:-s|ksh:-s|bash:--init-file|sh:--init-file|zsh:--init-file|dash:--init-file|ksh:--init-file) reads_stdin=1 ;;
                        bash:-c|sh:-c|zsh:-c|dash:-c|ksh:-c) has_program=1 ;;
                        python:-c|python:-m|python3:-c|python3:-m) has_program=1 ;;
                        node:-e|node:--eval|node:-p|node:--print|node:--check|node:-c) has_program=1 ;;
                        node:--env-file|node:-r|node:--require|node:--loader|node:--experimental-loader|node:--import|node:-C|ruby:-C|ruby:-E|perl:-I|perl:-M|ruby:-I|ruby:-r|python:-W|python3:-W|python:-X|python3:-X|bash:-o|sh:-o|zsh:-o|dash:-o|ksh:-o) skip_next=1 ;;
                        *:-*|*:+*) ;;
                        *:*/*|*:*.*) has_program=1 ;;
                        *) ;;
                    esac
                done
                if [ -n "$reads_stdin" ] || [ -z "$has_program" ]; then echo "interpreter stdin execution"; return 0; fi
                ;;
        esac
    fi

    # Block explicit proc-environ inputs. For grep, its first operand is a
    # pattern, so a quoted search string remains allowed.
    local proc_token proc_path proc_forced="" proc_reader="" grep_pattern=""
    local proc_scan="$rest"
    case "$name" in cat|less|more|head|tail|xxd|strings|cp|dd|od|grep|base64|tr) proc_reader=1 ;; esac
    # A redirect-first segment has no command word, so the operator is $name and
    # would never be scanned as an operand.  Scan it with the rest.
    case "$name" in '<'*|[0-9]'<'*) proc_scan="$name${rest:+ $rest}" ;; esac
    # Redirects peeled from BEFORE the command word are scanned too, after the
    # command's own operands so a `<` stays adjacent to its target
    # (`2>/dev/null exec </proc/self/environ` must not split them).
    [ -n "$leading_redirects" ] && proc_scan="$proc_scan${proc_scan:+ }${leading_redirects# }"
    case "$proc_scan" in *environ*)
        for proc_token in $proc_scan; do
            case "$proc_token" in \"*\") proc_token="${proc_token#\"}"; proc_token="${proc_token%\"}" ;; \'*\') proc_token="${proc_token#\'}"; proc_token="${proc_token%\'}" ;; esac
            case "$proc_token" in [0-9]'<'*) proc_token="${proc_token#?}" ;; esac
            proc_path="$proc_token"; case "$proc_token" in '<') proc_forced=1; continue ;; '<'*) proc_path="${proc_token#<}"; proc_forced=1 ;; if=*) proc_path="${proc_token#if=}"; proc_forced=1 ;; esac
            if [[ "$proc_path" =~ ^/proc/([0-9]+|self|thread-self)/environ$ ]] || { [ -n "$PROC_ENV_CWD" ] && [ "$proc_path" = environ ]; }; then
                if [ -n "$proc_forced" ] || { [ -n "$proc_reader" ] && { [ "$name" != grep ] || [ -n "$grep_pattern" ]; }; }; then echo "/proc environment read"; return 0; fi
            fi
            [ "$name" = grep ] && [ -z "$grep_pattern" ] && [[ "$proc_token" != -* ]] && grep_pattern=1
            proc_forced=""
        done
        ;;
    esac

    # Block Bash writes that TARGET a .claude/ path — the F1 self-neutralize
    # hole (an agent overwriting its own hooks/settings). Two mechanisms:
    #   1. Redirects into .claude:  echo x > .claude/settings.json  (>, >>, N>)
    #   2. Copy/move/write commands: cp/mv/tee/install/dd/rsync/ln/sed -i into it
    # Pipelines split on '|' so `... | tee .claude/x` lands here as a `tee ...`
    # segment. Reads of .claude (cat/grep) are left alone.
    #
    # A parameter-expanded destination (`> "$target"`, `cp x "$dst"`) cannot be
    # inspected here — Bash expands the variable only AFTER this guard runs, so
    # `target=.claude/... ; printf x > "$target"` would otherwise slip past the
    # literal `.claude` match. Fail closed on any '$' in a redirect/write target.
    case "$normalized" in
        *'>'*.claude*)
            echo ".claude write (redirect)"
            return 0
            ;;
        *'>'*'$'*)
            echo ".claude write (dynamic redirect target)"
            return 0
            ;;
    esac
    case "$command_text" in
        "cp "*|"mv "*|"tee "*|"install "*|"dd "*|"rsync "*|"ln "*|"sed -i"*)
            case "$normalized" in
                *.claude*)
                    echo ".claude write (copy/move/tee)"
                    return 0
                    ;;
                *'$'*)
                    echo ".claude write (dynamic target)"
                    return 0
                    ;;
            esac
            ;;
    esac

    # Ref-mutating git commands are allowed only from the worktree kew
    # assigned to this agent.  A child can `cd` to the operator checkout (or
    # use `git -C`) while still carrying a perfectly plausible issue branch;
    # checking the effective repository root and current branch closes that
    # wrong-cwd blast radius.  The marker is injected by kew, never by the
    # issue body or the agent.
    #
    # THIS RULE APPLIES TO DISPATCHED AGENTS ONLY.  `kew setup` installs this
    # guard at the PROJECT root, so it also loads for the human's own
    # interactive session — agents get it because their worktrees are checkouts
    # of the very same repo, and no deployment split can separate the two.  The
    # script therefore has to tell them apart, and AGENT_ASSIGNED_WORKTREE is
    # the signal: kew sets it on the agent process (orchestrator.run_agent, the
    # single spawn point) and nothing else sets it.  Its ABSENCE means no
    # dispatch is in progress, so there is no assignment to be outside of.
    # Treating absence as a violation locked the operator out of committing in
    # their own repository and reported it as "outside assigned worktree" — an
    # accusation about an assignment that never existed.
    #
    # Residual, accepted deliberately: an agent that spawns a nested Claude Code
    # with `env -u AGENT_ASSIGNED_WORKTREE` gets a child session treated as an
    # operator.  This hook is defence in depth, not the containment boundary —
    # the container is — and it only ever governed refs inside a checkout the
    # host is moving to stop trusting anyway.  Locking every user out of their
    # own repository is the larger harm.
    # Peel git's global options off the front of the command.  `-C` may repeat,
    # each path resolved against the one before it, so only the last one names
    # the repository git actually operates on.  Any other global option is left
    # unmodelled ON PURPOSE: `--git-dir=`, `--work-tree=` and `-c` can all
    # retarget the repository, so parsing stops there and the command counts as
    # unverifiable rather than being read as a plain `git <subcommand>`.
    git_ref_mutation=""
    git_mutation_command="$normalized"
    git_target_cwd="$COMMAND_CWD"
    git_parse_incomplete=""
    git_rest=""
    case "$normalized" in
        git\ *)
            git_rest="${normalized#git }"
            while :; do
                case "$git_rest" in
                    -C\ *)
                        git_rest="${git_rest#-C }"
                        case "$git_rest" in
                            \"*)
                                git_c_path="${git_rest#\"}"
                                git_c_path="${git_c_path%%\"*}"
                                git_rest="${git_rest#*\"*\"}"
                                git_rest="${git_rest# }"
                                ;;
                            \'*)
                                git_c_path="${git_rest#\'}"
                                git_c_path="${git_c_path%%\'*}"
                                git_rest="${git_rest#*\'*\'}"
                                git_rest="${git_rest# }"
                                ;;
                            *)
                                git_c_path="${git_rest%% *}"
                                git_rest="${git_rest#"$git_c_path"}"
                                git_rest="${git_rest# }"
                                ;;
                        esac
                        git_target_cwd=$(resolve_directory "$git_c_path" "$git_target_cwd" || true)
                        if [ -z "$git_target_cwd" ]; then
                            # An unresolvable -C target cannot be verified.
                            git_parse_incomplete=1
                            break
                        fi
                        ;;
                    -*)
                        git_parse_incomplete=1
                        break
                        ;;
                    *)
                        break
                        ;;
                esac
            done
            git_mutation_command="git $git_rest"
            ;;
    esac
    if [ -n "$git_parse_incomplete" ]; then
        # The repository is unknown, so a ref mutation anywhere in what is left
        # of the command must be denied rather than guessed at.
        case " $git_rest " in
            *" commit"*|*" reset"*|*" checkout -B "*|*" update-ref "*|*" branch -f "*|*" branch --force "*)
                echo "git ref mutation with an unresolvable target"
                return 0
                ;;
        esac
    fi
    case "$git_mutation_command" in
        git\ commit*|git\ reset*|git\ checkout\ -B\ *|git\ update-ref\ *|git\ branch\ -f\ *|git\ branch\ --force\ *)
            git_ref_mutation="$git_mutation_command"
            ;;
    esac
    # No marker => not a kew-dispatched agent => this rule has no subject.
    if [ -n "$git_ref_mutation" ] && [ -z "${AGENT_ASSIGNED_WORKTREE:-}" ]; then
        git_ref_mutation=""
    fi
    if [ -n "$git_ref_mutation" ]; then
        assigned_root=""
        actual_root=""
        if [ -n "${AGENT_ASSIGNED_WORKTREE:-}" ]; then
            if command -v realpath >/dev/null 2>&1; then
                assigned_root=$(realpath "$AGENT_ASSIGNED_WORKTREE" 2>/dev/null || printf '%s' "$AGENT_ASSIGNED_WORKTREE")
            else
                assigned_root="$AGENT_ASSIGNED_WORKTREE"
            fi
        fi
        if [ -n "$git_target_cwd" ]; then
            actual_root=$(git -C "$git_target_cwd" rev-parse --show-toplevel 2>/dev/null || true)
            current_branch=$(git -C "$git_target_cwd" symbolic-ref --short HEAD 2>/dev/null || true)
        else
            actual_root=""
            current_branch=""
        fi
        if [ -n "$actual_root" ] && command -v realpath >/dev/null 2>&1; then
            actual_root=$(realpath "$actual_root" 2>/dev/null || printf '%s' "$actual_root")
        fi
        case "$current_branch" in
            agent/issue-*)
                ;;
            *)
                echo "git ref mutation outside assigned worktree"
                return 0
                ;;
        esac
        if [ -z "$assigned_root" ] || [ "$actual_root" != "$assigned_root" ]; then
            echo "git ref mutation outside assigned worktree"
            return 0
        fi

        # Even inside an assigned worktree, never force or retarget a ref that
        # is not in the agent namespace.
        case "$git_ref_mutation" in
            "git checkout -B "*)
                target="${git_ref_mutation#git checkout -B }"
                target="${target%% *}"
                ;;
            "git update-ref "*)
                target="${git_ref_mutation#git update-ref }"
                target="${target%% *}"
                ;;
            "git branch -f "*)
                target="${git_ref_mutation#git branch -f }"
                target="${target%% *}"
                ;;
            "git branch --force "*)
                target="${git_ref_mutation#git branch --force }"
                target="${target%% *}"
                ;;
            *)
                target=""
                ;;
        esac
        if [ -n "$target" ]; then
            case "$target" in
                agent/issue-*|refs/heads/agent/issue-*)
                    ;;
                *)
                    echo "git ref mutation targets a branch outside agent/issue-*"
                    return 0
                    ;;
            esac
        fi
    fi

    for pattern in "${PREFIX_PATTERNS[@]}"; do
        case "$normalized" in
            "$pattern"*) echo "$pattern"; return 0 ;;
        esac
        case "$command_text" in
            "$pattern"*)
                echo "$pattern"
                return 0
                ;;
        esac
    done

    for pattern in "${EXACT_PATTERNS[@]}"; do
        case "$normalized" in
            "$pattern" | "$pattern "*) echo "$pattern"; return 0 ;;
        esac
        case "$command_text" in
            "$pattern" | "$pattern "*)
                echo "$pattern"
                return 0
                ;;
        esac
    done

    return 1
}

# Split command into lines, then by && || ; | and lone &.
block_reason=""
while IFS= read -r line; do
    next_is_pipe=""
    while IFS= read -r segment; do
        trimmed="${segment#"${segment%%[![:space:]]*}"}"
        case "$trimmed" in
            "|"|"|&") next_is_pipe=1; continue ;;
            "&&"|"||"|";"|"&") next_is_pipe=""; continue ;;
        esac
        if result=$(check_segment "$trimmed" "$next_is_pipe"); then
            block_reason="$result"
            break 2
        fi
        next_is_pipe=""
        track_cd_segment "$trimmed"
    done < <(echo "$line" | perl -pe 's/\s*(&&|\|&|(?<!>)\|\|?|;|(?<![>&|])&(?![&>]))\s*/\n$1\n/g')
done <<< "$COMMAND"

if [ -n "$block_reason" ]; then
    cat << EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED by security hook: '${block_reason}' is a destructive operation."}}
EOF
fi

exit 0

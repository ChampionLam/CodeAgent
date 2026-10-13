# install.ps1 -- CodeAgent one-shot installer for a bare Windows machine.
# Copies the app tree, bootstraps a private Python runtime (no admin,
# no system PATH, no registry) if the machine has no Python, installs
# requirements into a venv, optionally bootstraps a private Node
# runtime, writes the CodeAgent.bat launcher, and creates shortcuts.
#
# v2 (task t_1ce7a8ab, 2026-10-10). Changes over v1 (field-tested on a
# real zh-CN Windows 10 / PowerShell 5.1 box):
#   - All comments/output are English (repo is going open source).
#     Script must be saved as UTF-8 **with BOM** (verified lesson:
#     PS 5.1 parses a BOM-less .ps1 as GBK on zh-CN Windows; Chinese
#     comments can silently eat the next statement. This file is pure
#     ASCII, so it is safe either way, but the BOM rule is kept).
#   - Python 3.12 runtime is auto-bootstrapped into
#     <InstallDir>\runtime\python (python-build-standalone, npmmirror
#     mirror, sha256-verified) when no usable Python is found, so a
#     machine with NOTHING installed completes with one command.
#   - Node LTS is bootstrapped into <InstallDir>\runtime\node
#     (npmmirror, sha256-verified). FAIL-SOFT: the app itself runs on
#     the bundled electron.exe and does not need Node at runtime, so
#     Node failure is a warning, never an install failure. Node only
#     serves `npm start` fallback, UI rebuilds, and tests.
#   - True offline mode: -WheelDir (pre-downloaded pip wheels) plus
#     -PythonArchive / -NodeArchive (local archives) allow a fully
#     air-gapped install. -Offline now MEANS offline.
#   - WindowsApps python.exe (the Store stub that just opens the
#     Store) is detected and skipped.
#
#   - v3 (task t_7f45ebd3, 2026-10-10). Phase 0 environment check:
#     BEFORE touching anything, the installer probes the target machine
#     (OS/arch/admin, disk space, interpreters on PATH, existing
#     install, running processes, mirror reachability), prints an
#     aligned OK/MISSING/SKIP report table, and only then proceeds.
#     New -CheckOnly switch: run exactly that check, change NOTHING,
#     exit 0. All probes are read-only; nothing is written by Phase 0.
#     Normal installs also run Phase 0 first and log the same report.
#     NOT field-tested on a real Windows box (no Windows machine was
#     available to the author; syntax is parser-verified only).
#
#   - v4 (task t_5a787364, 2026-10-10). Field incident on a real box:
#     the source tree contains D:\...\ui\node_modules\codeagent -- a
#     JUNCTION pointing back at the repo root (npm workspace self-
#     link). robocopy follows junctions by default, so it copied the
#     repo into itself recursively (2021/1921 errors, 6.6 GB of junk
#     from a 1.1 GB tree). Fixes, ALL of them defensive because this
#     class of path bug cannot be caught on Linux:
#     a) every robocopy call now carries /XJ (never traverse
#        junctions) plus /XD on the known junction dir;
#     b) python\.venv is excluded from the copy (machine-specific
#        venv, its home points at the donor machine's interpreter --
#        observed E:\miniconda3 on the .5 box);
#     c) POST-COPY SANITY ASSERT: measured target bytes/files vs the
#        robocopy /L prediction; ratio > 1.5x or absurd growth ->
#        hard error, stop before the disk fills;
#     d) Phase 0 scans source and target for reparse points and
#        reports them, plus a disk-usage estimate (source copy size
#        + downloads) against free space.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -CheckOnly
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell ... -InstallDir "D:\My App" -DataDir D:\CodeAgentData
#   powershell ... -PythonArchive C:\Downloads\cpython-3.12.15+...-install_only.tar.gz
#   powershell ... -NodeArchive    C:\Downloads\node-v22.23.3-win-x64.zip
#   powershell ... -WheelDir       C:\wheelhouse -Offline   (full offline)
#   powershell ... -Uninstall [-PurgeData]
#     (v11: -Uninstall also removes OUR official-installer WiX
#      registration -- ARP + Installer\Dependencies -- by running the
#      official installer itself; /repair-then-/uninstall when the
#      files were already deleted. Registration for Pythons that are
#     NOT ours is reported and left untouched.)
#
# Recommended flow (see README): run -CheckOnly first, read the
# report, resolve anything it flags (disk space, mirror outage,
# running processes), THEN run the real install.
#
# VERIFICATION STATUS (be honest, this is an open repo):
#   * Field-tested on a real zh-CN Win10 / PS 5.1 box (v1 core):
#     file copy, venv, pip via Tsinghua mirror, self-check, launcher
#     --user-data-dir, single-instance lock.
#   * v2 additions (runtime bootstrapping, archives, WheelDir,
#     WindowsApps stub detection, English comments) are NOT yet run
#     on a real Windows box. They are syntax-checked with the PS 5.1
#     parser (ParseFile, zero errors) but marked "not field-tested".
#   * v3 (Phase 0 environment check, -CheckOnly) is NOT yet run on a
#     real Windows box. It IS executed on PowerShell 7.4.6 (Linux) as
#     a smoke test of the table rendering and control flow; the
#     Windows-specific probes (admin check, Get-PSDrive, WindowsApps
#     stub) only take their Windows paths on a real box. Syntax is
#     parser-verified (zero errors). Still marked "not field-tested".
#   * v4 (junction defense, /XJ, .venv exclusion, post-copy sanity
#     assert, Phase 0 reparse scan) is NOT yet run on a real Windows
#     box; the incident that motivated it WAS real, the fixes are
#     logic-reviewed and parser-verified only. Marked "not
#     field-tested" until the next real-machine install.
#   * v5 (task t_220c3782, 2026-10-10). Real-machine incident on the
#     v2 auto-bootstrap branch: the USTC github-release mirror's
#     archive URLs return the directory HTML page with HTTP 200
#     (262,161 bytes of text/html), which was saved as .tar.gz and
#     fed to tar.exe -> "Unrecognized archive format", install dead.
#     Also: its SHA256SUMS has 0 lines matching the current release,
#     and the "manifest missing" case only WARNED and installed an
#     unverified binary anyway. And: npmmirror's JSON listing masks
#     filenames mid-tag ("202****1009"), so masked names 404 on the
#     CDN. v5 fixes, driven by live probes of all endpoints from the
#     dev machine on 2026-10-10:
#     a) PBS source A is now the npmmirror date-directory layout:
#        latest ^\d{8}/ dir from the root JSON, entry from the date
#        dir's JSON, real filename = masked name with the **** run
#        replaced by the date (verified: 200, application/gzip,
#        46,352,482 bytes, magic 1F 8B);
#     b) PBS source B fallback: official python 3.12.9 installer exe
#        from huaweicloud (200, application/octet-stream, 26,923,696
#        bytes, magic 4D 5A), installed /quiet InstallAllUsers=0
#        TargetDir=<InstallDir>\runtime\python PrependPath=0
#        Include_pip=1;
#     c) EVERY runtime download is verified on three axes before
#        extraction: Content-Type, size band (from the same listing
#        that gave the URL), and magic bytes (gzip 1F8B / zip
#        504B0304 / exe 4D5A). Mismatch -> retry or fail; a file
#        that is not a real archive never reaches tar/Expand-Archive.
#        The node zip download got the same treatment.
#     d) sha256 is now a HARD requirement: no manifest -> REFUSE,
#        with -AllowUnverified as the only explicit override. The
#        checksum and the download always come from the SAME mirror
#        directory. (No manifest exists for source B's exe; there
#        the three-axis check is all we have, and the error text
#        says so.)
#     v5 runtime code paths are NOT field-tested on a real Windows
#     box yet; the mirror endpoints and hash claims above ARE live
#     probes, not guesses.
#   - v6 (task t_db62fa65, 2026-10-10). User decision: pin versions,
#     use the OFFICIAL python installer from Tsinghua/Aliyun mirrors.
#     a) Python bootstrap main path is now the official installer exe
#        python-3.12.10-amd64.exe (FIXED version, no dynamic resolve),
#        primary https://mirrors.tuna.tsinghua.edu.cn/python/3.12.10/
#        backup https://mirrors.aliyun.com/python-release/windows/
#        both live-verified 200 / application/octet-stream /
#        26,964,224 bytes. Verification: Content-Type + size band
#        26.5-27.5 MB + MZ magic + AUTHENTICODE SIGNATURE (the
#        official installer is signed by Python Software Foundation;
#        mirrors carry no SHA256SUMS, so the signature check is the
#        integrity guarantee). Fallback chain: Tsinghua -> Aliyun ->
#        hard error with actionable pointers (-PythonArchive local
#        archive / manual install).
#     b) python-build-standalone is now an OPT-IN switch (-UsePbs),
#        no longer the default; the USTC route is REMOVED entirely
#        (its URLs serve an HTML page with HTTP 200 -- dead).
#     c) Node is pinned to v22.20.0 (fixed; no index.tab resolve
#        unless -NodeVersion is explicitly overridden), downloaded
#        from npmmirror with SHASUMS256.txt sha256 (hard requirement)
#        + size band 34-37 MB + zip magic. Still fail-soft.
#     d) venv gets a pip.ini pinning the Tsinghua index (index-url +
#        trusted-host), so any later manual pip install inside the
#        venv also uses the mirror.
#     e) -Uninstall also removes the per-user uninstall registry
#        entry the official installer registered for OUR TargetDir
#        (matched by InstallLocation == <InstallDir>\runtime\python,
#        never touching other Pythons).
#     f) Phase 0 report states the exact pinned versions 3.12.10 /
#        v22.20.0, "official installer", and the ~27 MB + ~36 MB
#        expected download sizes.
#     Windows execution paths remain NOT field-tested (this dev
#     machine is Linux); mirror endpoints and sizes are live probes.
#   - v7 (task t_ebd450ff, 2026-10-10). New -PackageDir: a single
#     directory where ALL downloadables land and are REUSED across
#     machines and re-installs.
#     a) -PackageDir defaults to <InstallDir>\packages. Every
#        bootstrap download (python-3.12.10-amd64.exe, the PBS
#        tar.gz when -UsePbs, node-v22.20.0-win-x64.zip,
#        SHASUMS256.txt) is saved there under its canonical name.
#        At startup the installer checks the package dir FIRST: a
#        present file that still passes the SAME verification
#        (content-type is only checked on download; local reuse
#        checks size band / magic / signature / sha256) is used
#        directly; only a missing or bad file is downloaded. This
#        is the offline-move path: copy the package dir once, feed
#        N machines.
#     b) -PackageDir may point at READ-ONLY media (a CD/DVD share,
#        a write-protected dir): the installer never fails because
#        it cannot write there -- it uses what it can read and only
#        downloads when a package is missing AND the dir is
#        writable. Writability is probed with a temp file that is
#        removed immediately (fail-soft; -CheckOnly never probes
#        writes at all).
#     c) -WheelDir is kept; its default is now <PackageDir>\wheels
#        (an explicit -WheelDir always wins).
#     d) Phase 0 now reports the FINAL resolved InstallDir /
#        PackageDir / DataDir (existence + drive free space each),
#        and the package inventory: which packages are present /
#        missing, with version, size, and verification verdict
#        (local reuse re-runs size/magic/sha/signature checks).
#     e) Whole-script path hygiene: every filesystem touch on
#        user-supplied paths goes through -LiteralPath semantics
#        (Test-Path/Get-Item/New-Item/Remove-Item/robocopy args),
#        so paths with spaces ("D:\My Packages\runtime") and
#        non-ASCII directory names work everywhere. robocopy gets
#        quoted arguments built from Join-Path (v7).
#     f) README gained a "three typical usages" section.
#     v7 logic is smoke-tested on pwsh 7 (Linux) including a
#     package-dir reuse simulation with spaces + CJK chars; real
#     Windows execution remains NOT field-tested.
#   - v8 (task t_1b4879b0, 2026-10-10). Field findings on the .5 box
#     (real -CheckOnly / -Uninstall runs): the interpreter discovery
#     was PATH-ONLY -- E:\miniconda3\python.exe and a registry-
#     registered Anaconda were invisible, so a machine WITH Python
#     still downloaded a 27 MB installer. Also: disk-space rows said
#     SKIP whenever the target dir did not exist yet, and -Uninstall
#     left a dangling HKCU PythonCore\3.12 entry pointing at the
#     deleted runtime.
#     a) Get-InterpreterDecision: registry sweep (HKCU PythonCore,
#        HKLM PythonCore, HKLM WOW6432Node PythonCore, InstallPath
#        default value) + py launcher -0p listing + bounded common-
#        location scan (conda roots on every fixed drive,
#        %LOCALAPPDATA%\Programs\Python, %ProgramFiles%\Python3*,
#        C:\Python3*). EVERY candidate exe is actually executed
#        (-c "import sys;print(sys.version)") before being trusted;
#        conda BASE environments are skipped by default (shared,
#        often 2GB+ installs the app must not tie itself to).
#        Selection order: -PythonPath > <InstallDir>\runtime\python >
#        venv > PATH >= 3.10 > registry/common >= 3.10 > bootstrap.
#        New -ForceOwnRuntime ignores all pre-existing interpreters
#        and installs the pinned runtime.
#     b) Phase 0 interpreter rows now list EVERY found interpreter
#        (source, version, selected or not, and the reason when not:
#        version < 3.10 / Store stub / conda base), plus an explicit
#        "will use" line.
#     c) Disk-space probes now resolve the drive letter from the
#        path even when the directory does not exist yet (drive char
#        + Get-PSDrive), so a fresh install reports its target
#        volume's free space instead of SKIP.
#     d) -Uninstall also removes the HKCU\Software\Python\PythonCore
#        entry OUR installer registered, matching strictly on
#        InstallPath under <InstallDir> (never the user's own
#        registrations), with a read-back verification printout.
#     v8 discovery/uninstall logic is smoke-tested on pwsh 7 (Linux)
#     with registry-free simulated candidates; Windows registry and
#     conda-scan paths are parser-verified only. NOT field-tested.
#   - v9 (task t_1fea7924, 2026-10-10). SECURITY fix (field incident,
#     .5 box, 2026-10-10 12:0x): a fresh install copied the source
#     tree's REAL .env (240 bytes, live keys) and REAL config.json
#     (2311 bytes, model + api key fields) into <InstallDir> --
#     robocopy's exclusion list did not mention them, so shipping the
#     install dir meant shipping the user's API keys.
#     a) Every app-tree robocopy now carries /XF .env config.json
#        .install-state.json *.log (name-based, whole tree). v8's
#        node_modules robocopy is untouched (no credentials live
#        there) but keeps /XJ.
#     b) HARD post-copy assertion (Assert-BlankConfig) -- the key
#        defense line, NOT just the exclusion list: after a fresh
#        copy, <InstallDir>\.env must exist and be 0 bytes or
#        comment-only; <InstallDir>\config.json must be absent or
#        byte-identical to config.example.json (or contain no
#        non-empty api_key/apiKeyEnv field). Any non-comment
#        content in .env or any live key value -> the install FAILS
#        with "credentials leaked from source tree". A config.json/
#        .env that already existed at the target BEFORE the copy is
#        user-owned and is left untouched (assert skipped for it).
#     c) config.json is generated from config.example.json when
#        absent (all placeholder, no usable model, no key); .env is
#        written as a 0-byte UTF-8-no-BOM file when absent.
#     d) Phase 0 reports source .env / config.json detection and
#        states they are EXCLUDED from the copy + that the target
#        starts from a BLANK config the user must fill in.
#     e) The "install complete" block always prints, for a fresh
#        install: "config is BLANK -- edit config.json (models) and
#        .env (keys) before use".
#     f) README gained a "first-run configuration" section; no
#        example secrets anywhere.
#     v9 logic is smoke-tested on pwsh 7 (Linux) via extracted
#     function tests; the Windows robocopy /XF behavior is
#     documented robocopy semantics, parser-verified. NOT
#     field-tested on a real Windows box.
#   - v10 (task t_ddd7b1b7, 2026-10-10). Field incident on the .5
#     box (real runs, official installer logs in hand): running the
#     official python-3.12.10-amd64.exe with /quiet returned exit
#     code 0 but installed NOTHING, twice, for two reasons:
#     (a) a same-version registration already existed -> the WiX
#         bundle log says "WixBundleInstalled = 1" ("already
#         installed", no action), TargetDir silently ignored, the
#         target dir is never created;
#     (b) a DANGLING registration (registry says installed, files
#         deleted): the Detect phase shows core_JustForMe: Present,
#         exe_JustForMe: Present, dev_JustForMe: Present,
#         lib_JustForMe: Present -> the same silent skip.
#         /uninstall alone returns 1603 in that state; /repair
#         (exit 0) restores the files, THEN /uninstall succeeds.
#     CORE FACT (stated here AND in the README): the official
#     installer is a per-version SINGLE INSTANCE -- it cannot
#     install a second copy of the same version on one machine.
#     Fixes:
#     a) New Get-RegisteredPythonInstall: pre-install registry
#        sweep for the PINNED version's registration (HKCU/HKLM/
#        WOW6432Node PythonCore\<tag>\InstallPath; ARP Uninstall
#        DisplayName; HKCU\Software\Classes\Installer\Dependencies).
#        - registration present AND its interpreter actually runs
#          the pinned version -> REUSE it as the venv base; the
#          message says "already registered, reusing, no download".
#        - registration present but the interpreter is missing or
#          broken -> HARD ERROR with the executable fix recipe
#          (/repair /quiet then /uninstall /quiet, or manual
#          cleanup). Never a silent continue.
#        - -ForceOwnRuntime + same-version registration -> HARD
#          ERROR: a second copy cannot be laid; use -UsePbs or a
#          different patch version (e.g. 3.12.9).
#     b) The official installer now ALWAYS runs with
#        /log <writable-dl-dir>\python-installer.log, and on ANY
#        failure (rc != 0, or the existing "reported success but
#        python.exe missing" hard assert firing) the log is scanned
#        and three line families are printed: WixBundleInstalled,
#        Detect-phase package states (Present/Absent), and the
#        final Exit code. Those three lines are exactly what located
#        this root cause; users triaging later need them.
#     c) Phase 0 gained an "official <version> registration" row
#        (clear / reusable / broken) and the conclusion block adds
#        FIX FIRST items for a broken registration and for
#        -ForceOwnRuntime colliding with a same-version
#        registration.
#     d) README "first-run configuration" and "runtime" sections
#        now both state the two facts: an already-registered same
#        version is reused; the official installer cannot lay a
#        second copy of the same version.
#     v10 registry-sweep logic is smoke-tested on pwsh 7 (Linux)
#     with simulated registry paths; real Windows registry
#     enumeration is parser-verified only. NOT field-tested.
#     v11 logic is smoke-tested on pwsh 7 (Linux) with the v10
#     fake-registry harness extended to the uninstall functions;
#     the real installer /uninstall + /repair chain and Windows
#     registry enumeration are parser-verified only. NOT
#     field-tested.
#   - v11 (task t_2450a5b7, 2026-10-10). Field finding on the .5
#     box: -Uninstall removed the install dir and the PythonCore
#     entry but LEFT the official installer's OWN WiX registration
#     (ARP "Python 3.12.10 (64-bit)" + 8 HKCU\Software\Classes\
#     Installer\Dependencies entries) -- the exact residue that
#     makes the NEXT same-version install silently skip
#     (WixBundleInstalled). Also: users asked how to opt in to a
#     conda BASE python. Fixes:
#     a) -Uninstall now runs the official installer itself to
#        remove its registration: /uninstall /quiet (in the
#        files-already-deleted state a direct /uninstall returns
#        1603; /repair /quiet (exit 0) restores consistency first,
#        then /uninstall succeeds -- field-verified sequence). The
#        installer exe comes from the package-dir cache, else a
#        verified download (the SAME 4-axis check chain as the
#        install path: content-type / size band / MZ magic /
#        Authenticode PSF). Logs land in the package dir.
#     b) OWNERSHIP: the registration is only cleaned when one of
#        its surfaces (PythonCore InstallPath / ARP
#        InstallLocation) points INSIDE <InstallDir>; every
#        surface is printed with its verdict ("NOT ours, left
#        untouched" for the user's own Python). If nothing points
#        inside, the installer is never run.
#     c) Post-uninstall verification: the PythonCore / ARP / WiX
#        Dependencies counts for the pinned version must ALL be 0.
#        Any residue -> the uninstall FAILS (exit 1) with a
#        manual-cleanup recipe. /repair may have re-created
#        <InstallDir>\runtime -- it is stripped again.
#     d) New -AllowCondaBase switch: conda BASE is still skipped
#        by default (shared install), but users can opt in
#        explicitly. The skip reason and the Phase 0 report both
#        mention the switch and -PythonPath.
#   - v12 (task t_8ac08178, 2026-10-10). FIELD REGRESSION on the
#     a bare Windows box (real Windows PowerShell 5.1): v11's
#     registration evaluation aborted with "LiteralPath parameter
#     cannot bind" -- provider-qualified registry paths
#     ($tag.PSPath / $dep.PSPath, i.e. "Registry::HKEY_...")
#     were passed to Get-ItemProperty -LiteralPath, which PS 5.1
#     rejects. The pwsh 7 fake-registry harness never reproduces
#     this binding behavior, so it shipped unnoticed. Fixes:
#     a) EVERY registry read via $x.PSPath now uses -Path (the
#        field-verified working form; the ARP reads already did
#        and worked on the same machine). Filesystem -LiteralPath
#        usage is unchanged and correct.
#     b) Self-check hard gate (G1v12-G3v12): every -LiteralPath
#        argument in the script is statically scanned -- any
#        argument containing .PSPath, "::" or HKEY_ is a hard
#        harness failure, so this class of bug cannot ship again.
#     The field regression above is the ONLY behavior change;
#     all v11 baselines are preserved (66/66 self-checks pass).
#   - v13 (task t_9e01100c, 2026-10-10). SECOND FIELD REGRESSION on
#     a bare Windows box (PS 5.1): -Uninstall aborted with
#     "LiteralPath parameter cannot bind ... empty string" from
#     inside the registration-evaluation try/catch. Root cause
#     (field-reproduced locally): Windows PowerShell 5.1 THROWS on
#     Test-Path -LiteralPath '' (pwsh 7 returns False -- the Linux
#     harness could never see it), and in the uninstall path
#     $PackageDir is STILL "" when the user did not pass it (the
#     param-defaulting block runs after `exit`; it only serves the
#     install path). Fixes:
#     a) The uninstall path now defaults $PackageDir to
#        <InstallDir>\packages BEFORE first use, and every
#        Test-Path -LiteralPath whose argument is a variable (and
#        therefore possibly empty) goes through the new
#        Test-PathSafe helper (null/empty check first).
#     b) OWNERSHIP REDESIGN: the per-user official installer NEVER
#        writes an ARP InstallLocation, so v12's gate (which keyed
#        on it) abandoned all 8 ARP + 8 WiX Dependencies entries.
#        "Ours" is now decided ONLY by PythonCore\<tag>\InstallPath
#        pointing inside <InstallDir>. When ours, the official
#        installer chain (/uninstall, /repair-then-uninstall on
#        1603) removes PythonCore + ARP + WiX Dependencies together
#        (field-verified: repair 0 -> uninstall 0 -> all 0). When
#        not ours (someone else's same version), NOTHING is touched
#        -- not even printed as candidates for cleanup.
#     c) Registry reads use plain drive-qualified paths
#        ("HKCU:\...\<tag>\InstallPath" built with Join-Path) with
#        Get-ItemProperty -Path; NO provider-qualified $x.PSPath
#        anywhere (hard static gate G1v13), NO registry path ever
#        passed to -LiteralPath (v12 gates kept).
#     d) Residue gate (all three surfaces must be 0, else exit 1),
#        the INCOMPLETE manual-cleanup recipe, the /repair-then-
#        re-strip of <InstallDir>, and all v12 baselines preserved.
# v14 (task t_5c458d9d): the v13 OWNERSHIP DIRECTION was right but
#     the comparison was wrong -- captured verbatim from the the field box
#     registry after an install into D:\t:
#       HKCU\Software\Python\PythonCore\3.12\InstallPath
#         (default)      = "D:\t\runtime\python\"   <- TRAILING
#                                                         BACKSLASH
#         InstallPath    = ""                            <- EMPTY
#     The official installer writes the path as the (default) value
#     with a trailing backslash; v13 compared raw strings and never
#     matched, so our own registration read as "not ours" (ARP/WiX
#     entries never cleaned; Phase 0 could also misreport it
#     BROKEN while the interpreter was perfectly alive). Fixes:
#     a) NEW Get-PythonCoreInstallPath: the SINGLE place that reads
#        a PythonCore <tag>\InstallPath value -- both (default) and
#        the named InstallPath, preferring the non-empty one, TRIMMED.
#        All three registry sweeps (Get-PythonCandidates,
#        Get-RegisteredPythonInstall, Test-RegistrationIsOurs) now
#        go through it (static gate G1v14: exactly one '(default)'
#        read left in code).
#     b) NEW Convert-PathForCompare + rewritten Test-PathUnder:
#        both sides are Trim()'d, trailing separators stripped,
#        separators unified, and on Windows resolved via
#        [IO.Path]::GetFullPath; comparison is case-insensitive
#        "prefix + separator" (equal counts as ours). A trailing
#        backslash in the registry value can no longer break
#        ownership.
#     c) Uninstall cleanup flow, residue gate (three surfaces must
#        be 0, else exit 1), and all v12/v13 gates preserved.
#        NOT field-tested yet on a real Windows box (Linux
#        self-check harness only).
#   - v20 (task t_ed26cc98, 2026-10-13). THIRD FIELD REGRESSION on the
#     a bare Windows box (fresh dir D:\t\CodeAgent, install.ps1 v19):
#     a) CRASH (seconds-in, uncaught): Get-RegistrySubKeys returns
#        `,$out` with $out=@() -- a ONE-ELEMENT ARRAY containing an
#        EMPTY ARRAY. foreach then iterates that empty-array ELEMENT
#        (not zero iterations), and Split-Path -Leaf <empty> throws
#        ParameterArgumentValidationErrorEmptyArrayNotAllowed
#        (reproduced on pwsh 7.4.6 Linux, /tmp/repro_empty_root.ps1).
#        Trigger: HKCU\Software\Python\PythonCore EXISTS with ZERO
#        subkeys (left-behind root after a real uninstall / registry
#        cleaner). Observed at line 1119; the SAME pattern sits at
#        965-966, 1118-1119, 1617-1618, 1683-1684. Fix: helper now
#        appends only REAL strings (skips any non-string/blank item,
#        so an unrolled empty array never enters $out), AND every
#        caller guards with `Where-Object { $_ }` (defense in depth,
#        both layers independently sufficient).
#     b) DEAD-END (install stops): "REGISTERED but broken" hard-threw
#        with a repair recipe that does NOT work on the real box
#        (/repair /quiet TargetDir=... -> 1602; /uninstall /quiet ->
#        1603 -- field log D:\t\CodeAgent.log 01:50-02:10). Fix: the
#        broken registration is no longer terminal. When -UsePbs is
#        given, we now FALL THROUGH to the portable PBS route (no
#        registration read or written); otherwise the installer
#        continues with the OTHER routes (PBS auto-try, -PythonPath,
#        pre-seeded archive) before stopping. NEW -IgnoreRegistryState
#        switch: skip the registration gate entirely (explicit
#        user opt-in, loud warning, registry never touched).
#     c) SILENT DOWNGRADE (-UsePbs): TWO independent resolver bugs,
#        both live-verified 2026-10-13 against npmmirror:
#        - npmmirror keeps only the NEWEST patch of each minor in
#          the LATEST date dir (20261009/ = 3.12.15 ONLY); pinned
#          3.12.10 lives in an OLD dir (20250529/, masked name
#          cpython-3.12.10+202****0529-..., SHA256SUMS present).
#          v19 resolved ONLY the latest dir.
#        - the filename regex tail '\.\d+\+' NEVER matched a full
#          x.y.z pin (real names are "cpython-3.12.10+202****...",
#          '+' directly after the patch, no extra ".NN" segment).
#        Net effect: "could not resolve ... falling back to the
#        official installer path" -> registry-writing installer ran
#        with no notice. Fix: Get-PbsArchive walks date dirs
#        NEWEST-FIRST until it finds the pinned version, the regex
#        patch segment is optional ((?:\.\d+)?\+), and the fallback
#        message is now LOUD and explicit (states the registry
#        consequence, tells the user how to stop).
#   - v21 (task t_69654390, 2026-10-13). FOURTH FIELD REGRESSION on
#     the field box (2026-10-13 09:10, install.ps1 v20-era): TWO gaps.
#     a) LEAK HOLE in the blank-config assertion: the copy excluded
#        .env / config.json but NOT their dev-era backups -- a real
#        config.json.bak-20261009-173158 (2894 B, apiKey/DASHSCOPE
#        field names) rode along to a fresh install while
#        Assert-BlankConfig only checked .env and config.json. Fixes:
#        /XF list extended to backup/credential PATTERNS (*.bak*,
#        *.old, *.orig, *.backup, .env.*, config.json.*, *.key,
#        *.pem, *credentials*, *secret*), AND the assertion is now a
#        CONTENT sweep of the WHOLE install tree (Assert-BlankConfig):
#        every text-looking file (binary = NUL in first 8 KB is
#        skipped; >10 MB skipped) is scanned for literal sk- key
#        material and every *.json is parsed with non-empty
#        api-key/token/secret/credential/password fields -> hard
#        throw "credentials leaked from source tree".
#     b) DEV JUNK in the install dir: .pytest_cache/, backup-deploy-*,
#        backup-flow-*, backup-ui-*, docs/, tests/, .github/,
#        tsconfig.test.json landed in D:\...\CodeAgent. Fixes: /XD
#        name-based exclusions (tests, test, __pycache__,
#        .pytest_cache, cache dirs, backup-*, backups) +
#        root-specific docs/ .github/; tsconfig.test.json in /XF;
#        the sanity-check exclude list mirrors the copy set; the
#        completion report now PRINTS the actual top-level layout and
#        warns if any dev residue is still present.
#     v21 is smoke-tested on pwsh 7 (Linux) via v21-selfcheck.ps1
#     (structure gates + behavior tests of the credential sweep and
#     exclusion list); real Windows robocopy /XD /XF semantics are
#     documented robocopy behavior, parser-verified. NOT
#     field-tested on a real Windows box yet.
#   - v23 (task t_d37fca26, 2026-10-13). FIFTH FIELD REGRESSION
#     (field box, 2026-10-13 14:0x, v21-era sweep): SECURITY FALSE
#     POSITIVE. The v21 credential sweep's live-field regex
#     `.*_?token` matched BARE field names, so a shipped model
#     artifact -- python\models\decision_bert\tokenizer.json
#     (268,943 B) -- tripped it on `unk_token` and the installer
#     hard-stopped. HuggingFace tokenizer.json ALWAYS contains
#     unk_token / pad_token / cls_token / mask_token; any tree that
#     ships the guardian model can never install. Fixes (precision,
#     not removal -- the gate stays):
#     a) field name is now an EXACT allowlist of credential names
#        (api_key, access_token, secret_key, client_secret,
#        password, ...), not a fuzzy `.*_?token` suffix match;
#     b) the VALUE must also look like a secret: length >= 16 or a
#        sk- key shape or high entropy-ish charset; short symbolic
#        tokens ("[UNK]", "[PAD]", token ids) never match;
#     c) model artifacts are exempt: *.onnx, *.onnx.data,
#        tokenizer.json, vocab.json, vocab.txt, special_tokens_map.json,
#        added_tokens.json, merges.txt, spiece.model, tokenizer_config.json;
#     d) regression samples added to the v23 selfcheck:
#        tokenizer.json -> pass, config.json.bak with sk- -> THROW,
#        empty template config.json + 0-byte .env -> pass.
#     Changelog normalization note: the on-disk script was at v22
#     content (t_a8ceb810) with only a v21 header block; this entry
#     is numbered v23 to keep the ledger monotonic.
#   - v24 (task t_ab909828, 2026-10-13). FOURTH FIELD REGRESSION:
#     install from a clone/zip source finished GREEN, the app opened
#     to a BLACK WINDOW. Root cause: the app's launch-critical files
#     are gitignored BUILD PRODUCTS -- dist/ (.gitignore line 2) and
#     electron/ui/ (line 26) -- and .gitignore'd content never rides
#     along in a clone; the installer only WARNED ("target machine
#     will need 'npm install'") and completed. Fixes:
#     a) NEW BUILD STAGE (Invoke-AppBuild) after the Node bootstrap,
#        online installs by default: npm install (root, npmmirror
#        registry + ELECTRON_MIRROR) -> npm run build (dist\electron\
#        + electron\renderer.bundle.js) -> scripts\build-ui.ps1
#        (ui build + stage ui\dist -> electron\ui). All npm runs use
#        the installer's OWN Node (<InstallDir>\runtime\node on
#        PATH), never a system Node. Each step is idempotent (skips
#        when its artifact already exists).
#     b) HARD ARTIFACT GATE before the launcher is written: all three
#        of dist\electron\main.js, electron\ui\index.html,
#        node_modules\electron\dist\electron.exe must exist, else the
#        install FAILS with the manual remedy printed. The old
#        behavior (warn + install a dead app) is gone.
#     c) NEW -SkipBuild switch: skip the build (with -Offline it is
#        implied), each with explicit "the installed app WILL NOT
#        START as-is" warnings and the manual build recipe.
#     d) The FALSE comment at the launcher step (claiming the compiled
#        main is repo content) is corrected.
#     e) Completion report now prints build state (YES/NO) and the
#        UI artifact path. The GUI wizard (install-gui.ps1) calls
#        this same script and inherits everything.
#   - v25 (task t_27714fcb, 2026-10-13). DATA DIR INTO THE INSTALL
#     TREE. Field finding: logs + sessions.db landed in
#     %APPDATA%\CodeAgent\data -- outside the "one movable/deletable
#     directory" promise. Fixes (installer/template side only; the
#     python engine is untouched):
#     a) -DataDir now DEFAULTS to <InstallDir>\data (was: empty =
#        Electron default %APPDATA%\CodeAgent). Explicit -DataDir
#        still wins (existing usage preserved).
#     b) Launcher now always passes BOTH --user-data-dir="<InstallDir>
#        \userdata" (Chromium profile: Cache/Network/Preferences) and
#        DESK_AGENT_DATA_DIR="<InstallDir>\data" (sidecar data:
#        logs/sessions.db/audit.db). Split per t_27714fcb comment:
#        avoids the <InstallDir>\data\data double nesting, keeps user
#        data vs browser cache distinguishable at a glance.
#     c) electron/main.ts (glue only) now RESPECTS an inherited
#        DESK_AGENT_DATA_DIR instead of overwriting it with
#        <userData>\data; no inherited value -> legacy behavior.
#     d) ROBOCOPY SAFETY: the source repo's own data\ dir (dev
#        sessions.db / audit.db) is excluded from the app-tree copy
#        both by full source path and by /XF sessions.db / audit.db,
#        so a re-install can never clobber <InstallDir>\data.
#     e) UNINSTALL ORDER: the install-dir delete now runs AFTER the
#        -PurgeData gate; when the data dir is inside the InstallDir,
#        everything under it is removed with the tree (no -PurgeData
#        needed -- the tree IS owned), and %APPDATA%\CodeAgent is
#        never newly stamped for out-of-tree installs (v22 rule
#        kept).
#     f) Legacy-user compat: <InstallDir>\data absent while
#        %APPDATA%\CodeAgent exists non-empty -> no auto-move, no
#        delete, one final-report line telling the user to move it
#        manually if they want.
#     g) config.example.json: the filesystem MCP template entry is
#        now enabled:false (spawn-guaranteed-to-fail placeholder
#        path /path/to/allow/root + npx; field logs: mcp[filesystem]
#        discover failed / exited during initialize).
#   - v26 (task t_2d0783e3, 2026-10-13). DEFAULT WORKING DIRECTORY.
#     Requirement (user, 2026-10-13, translated from Chinese): "the
#     initial working directory defaults to <InstallDir>\workspace
#     and can be changed by the user in a conversation". Fixes:
#     a) New -WorkspaceDir param (explicit wins); default = resolved
#        <InstallDir>\workspace. The installer CREATES it with a
#        README.txt (UTF-8 no BOM) and writes the absolute path into
#        config.json -> capabilities.workspaceRoot.
#     b) Non-overwrite rule: an existing NON-EMPTY
#        capabilities.workspaceRoot survives UNLESS this run passed
#        -WorkspaceDir explicitly. Blank/absent -> filled with the
#        default. capabilities section auto-created if missing
#        (Add-Member, PS 5.1 safe).
#     c) Atomic config write: temp file + Move-Item -Force. NOTE:
#        [IO.File]::Replace(tmp, dst, $null) was the first attempt
#        and REJECTS an empty backup-path string on .NET (caught by
#        the local pwsh selfcheck, never shipped).
#     d) Completion report prints the ACTUAL resolved workspace path
#        + whether the config write happened ("workspace:" line).
#     e) Engine side (same task, python/electron): new set_workspace
#        tool + workspace.set RPC let the agent change the default
#        in-conversation (persisted to the same config key); electron
#        main.ts reads config.json capabilities.workspaceRoot as the
#        default workspaceRoot for chat/hub/info instead of the
#        hardcoded <userData>\workspace.
param(
    [string]$InstallDir = "",
    # v7: unified package/archive directory. Defaults to
    # <InstallDir>\packages. Downloadables land here AND are reused
    # from here (see header notes). May be read-only: the installer
    # uses what it finds and only downloads when it can write.
    [string]$PackageDir = "",
    [string]$DataDir = "",
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [int]$ProbeTimeoutSec = 10,
    [switch]$CheckOnly,
    [string]$PythonPath = "",
    # Local archive paths for air-gapped installs. When set, they
    # replace the network download for that runtime.
    [string]$PythonArchive = "",
    [string]$NodeArchive = "",
    # Directory of pre-downloaded pip wheels (pip install --no-index
    # --find-links). Implies offline pip.
    [string]$WheelDir = "",
    # v19: SELF-CONTAINED package caches. Opt-in; "" (default) keeps
    # the legacy machine-global cache behavior byte-for-byte. When
    # set, the installer creates <CacheDir>\pip and <CacheDir>\npm and
    # points pip's cache-dir (env + the venv's pip.ini) and npm's
    # cache at them, so the whole install directory can be moved or
    # deleted without leaving cache residue on the machine.
    [string]$CacheDir = "",
    # v26 (t_2d0783e3): default working directory. Empty = the new default
    # <InstallDir>\workspace. Explicit -WorkspaceDir "wins" (it is this run's
    # explicit user intent, so it IS written to config.json even when a
    # config.json already exists). With no -WorkspaceDir, an existing
    # non-empty capabilities.workspaceRoot is the user's earlier choice and is
    # NEVER overwritten; a blank/absent value is filled with the resolved
    # default. The directory is created (with a small README) either way.
    [string]$WorkspaceDir = "",
    # Mirror roots. Defaults target mainland China; override for
    # other regions.
    # PbsMirror (v5): npmmirror date-directory layout, resolved
    # dynamically (see Get-PbsArchive). The USTC github-release
    # LatestRelease route was abandoned: its archive URLs serve the
    # directory HTML with HTTP 200 (real-machine incident, field
    # report 2026-10-10) and its SHA256SUMS does not cover the
    # current release.
    [string]$PbsMirror = "https://registry.npmmirror.com/-/binary/python-build-standalone/",
    # Official-installer source chain (v6 default path). Tsinghua
    # first, Aliyun second; both live-verified 2026-10-10 (200,
    # application/octet-stream, 26,964,224 bytes). Version is the
    # PINNED constant below -- these are just mirror roots.
    [string]$PythonInstallerMirror = "https://mirrors.tuna.tsinghua.edu.cn/python/",
    [string]$PythonInstallerMirror2 = "https://mirrors.aliyun.com/python-release/windows/",
    [string]$NodeMirror = "https://registry.npmmirror.com/-/binary/node",
    # Version selection. v6: PINNED, no dynamic latest resolution.
    # Python 3.12.10 is the newest 3.12.x the Tsinghua mirror carries
    # (3.12.11 404s; live-checked). Node v22.20.0 is the fixed pick.
    # -PythonVersion / -NodeVersion remain as explicit overrides only.
    [string]$PythonVersion = "3.12.10",
    [string]$NodeVersion = "v22.20.0",
    # v6: python-build-standalone is OPT-IN (was the v5 default main
    # path). USTC route is deleted (serves HTML with HTTP 200).
    [switch]$UsePbs,
    # True fixed constants (not user-tunable): expected download sizes.
    # Python 3.12.10 amd64 installer: 26,964,224 bytes (both mirrors,
    # live-checked). Node v22.20.0 win-x64 zip: 35,500,968 bytes
    # (npmmirror, live-checked). Verification bands are wider.
    [switch]$Offline,
    [switch]$Force,
    [switch]$NoShortcut,
    [switch]$NoNode,
    [switch]$AllowUnverified,
    # v8: force the installer's OWN pinned runtime even when a usable
    # Python >= 3.10 is already installed on the machine.
    [switch]$ForceOwnRuntime,
    # v11: allow a conda BASE interpreter to be selected for the
    # venv. Skipped by default -- base is a shared install.
    [switch]$AllowCondaBase,
    [switch]$Uninstall,
    # v17: purge the data dir on -Uninstall. The target is resolved
    # with the SAME rules as the install side (explicit -DataDir >
    # install-time record > Electron default) and gated by an
    # ownership proof -- when we cannot prove the dir belongs to THIS
    # install we REFUSE to delete it (see Confirm-PurgeDataOwnership).
    [switch]$PurgeData,
    # v17: non-interactive mode. Every interactive prompt (directory
    # choice, not-empty confirm, pre-install "continue anyway") is
    # auto-answered with its default instead of hanging on Read-Host
    # (a real hazard when stdin is closed/NUL on real-machine runs).
    [switch]$Yes,
    # v20: treat the pinned-version REGISTRATION STATE as absent:
    # no "registered but broken" gate, no reuse, no conflict -- the
    # official-installer registration check is skipped entirely.
    # Meant for dangling registrations whose repair/uninstall both
    # fail (field: /repair -> 1602, /uninstall -> 1603). The
    # registry itself is NEVER written by this switch (only the
    # official installer exe route writes it, as before).
    # v24 (t_ab909828): install WITHOUT build used to finish "green"
    # and the app then opened to a BLACK WINDOW (clone/zip source has
    # no dist/ and no electron/ui/ -- both are gitignored build
    # outputs). Now the installer BUILDS them itself (npm install +
    # npm run build + build-ui.ps1) using its own Node, and a missing
    # artifact is a HARD error. -SkipBuild / -Offline keep the old
    # behavior but say plainly that the installed app will not start.
    [switch]$SkipBuild,
    [switch]$IgnoreRegistryState
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# constants / helpers
# ---------------------------------------------------------------------------
$Script:AppDirName = "CodeAgent"
$Script:PbsTag = ""   # resolved python-build-standalone release tag
# v6 pinned constants (see param block comments for live-verified sizes).
$Script:PyInstallerBytes = [long]26964224
$Script:NodeZipBytes     = [long]35500968

# ---------------------------------------------------------------------------
# v7: PackageDir helpers. All path handling is -LiteralPath semantics
# (Test-Path -LiteralPath, Join-Path, [IO.*] calls) so spaces and
# non-ASCII characters in user-supplied paths never break anything.
# ---------------------------------------------------------------------------

# Expand env vars and make a path absolute WITHOUT creating it and
# WITHOUT wildcard resolution (Get-Item -LiteralPath under the hood,
# unlike Resolve-Path which can choke on [ ] characters too).
function Resolve-PathSafe([string]$Dir) {
    if (-not $Dir) { return "" }
    $Dir = [Environment]::ExpandEnvironmentVariables($Dir)
    if (-not [IO.Path]::IsPathRooted($Dir)) {
        $Dir = Join-Path (Get-Location) $Dir
    }
    return $Dir
}

# Canonical package file name for each download (stable across
# machines -> a package dir copied to another box just works).
function Get-PackageFileName([string]$Kind) {
    # $Kind: "PythonInstaller" | "PbsArchive" | "NodeZip" | "NodeSums"
    switch ($Kind) {
        "PythonInstaller" { return "python-$PythonVersion-amd64.exe" }
        "PbsArchive"      { return "cpython-pbs-$PythonVersion-install_only.tar.gz" }
        "NodeZip"         { $v = $NodeVersion; if ($v -notmatch '^v') { $v = "v$v" }; return "node-$v-win-x64.zip" }
        "NodeSums"        { $v = $NodeVersion; if ($v -notmatch '^v') { $v = "v$v" }; return "node-$v-SHASUMS256.txt" }
    }
    throw "[install] unknown package kind: $Kind"
}

# Verify a LOCAL package file (no download happened, so no
# content-type). Same axes otherwise: size band + magic (+ caller
# adds signature/sha256 for its own kind). Returns $true/$false.
function Test-LocalPackage([string]$Path, [long]$ExpectedSize, [string]$MagicHex) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $len = (Get-Item -LiteralPath $Path -Force).Length
    if ($ExpectedSize -gt 0) {
        $lo = [long]($ExpectedSize * 0.5); $hi = [long]($ExpectedSize * 1.5)
        if ($len -lt $lo -or $len -gt $hi) {
            Write-Warning "[install] local package size is off (file: $len, expected ~$ExpectedSize): $Path"
            return $false
        }
    }
    if ($MagicHex) {
        $fs = $null
        try {
            $fs = [IO.File]::OpenRead($Path)
            $need = $MagicHex.Length / 2
            $buf = New-Object byte[] $need
            $got = $fs.Read($buf, 0, $need)
            if ($got -lt $need) { return $false }
            $actual = ([BitConverter]::ToString($buf)).Replace('-', '')
            if ($actual -ne $MagicHex) {
                Write-Warning "[install] local package magic bytes are $actual (expected $MagicHex): $Path"
                return $false
            }
        } catch { return $false }
        finally { if ($fs) { $fs.Dispose() } }
    }
    return $true
}

# Can we write into $Dir? Probes by creating+deleting a uniquely
# named temp file. Never called in -CheckOnly mode. Fail-soft: any
# error means "not writable" (read-only media, ACL, drive missing).
function Test-DirWritable([string]$Dir) {
    if (-not (Test-Path -LiteralPath $Dir)) { return $false }
    try {
        $probe = Join-Path $Dir ("." + [Guid]::NewGuid().ToString("N") + ".writetest")
        [IO.File]::WriteAllText($probe, "")
        Remove-Item -LiteralPath $probe -Force -ErrorAction Stop
        return $true
    } catch { return $false }
}

function Confirm-InstallDir([string]$Dir, [bool]$Force) {
    if (-not $Dir -or -not $Dir.Trim()) {
        throw "[install] install dir is empty; pass -InstallDir or answer the prompt"
    }
    $Dir = [Environment]::ExpandEnvironmentVariables($Dir)
    if (-not [IO.Path]::IsPathRooted($Dir)) {
        $Dir = Join-Path (Get-Location) $Dir
    }
    if (Test-Path -LiteralPath $Dir) {
        $existing = Get-ChildItem -LiteralPath $Dir -Force | Select-Object -First 3
        if ($existing -and -not $Force) {
            if ($Yes) {
                # v17: non-interactive -- -Yes auto-accepts the
                # overwrite (its whole point), matching -Force.
                Write-Host "[install] -Yes: directory '$Dir' is not empty -- proceeding (non-interactive)"
            } else {
                $answer = Read-Host "Directory '$Dir' is not empty. Install/overwrite into it? [y/N]"
                if ($answer -notmatch '^[Yy]') {
                    throw "[install] aborted by user; use -Force to skip this prompt"
                }
            }
        }
    } else {
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    }
    return (Resolve-Path $Dir).Path
}

# ---------------------------------------------------------------------------
# v5: npmmirror date-directory resolution + hard download verification.
#
# npmmirror mirrors python-build-standalone as JSON directory listings
# of DATE directories (e.g. 20261009/). Filenames inside a date dir are
# masked mid-tag: "cpython-3.12.15+202****1009-x86_64-pc-windows-msvc-
# install_only.tar.gz" -- the masked name itself is NOT a valid URL
# (404 on the CDN); the real object is the same name with the mask run
# replaced by the date directory name. Resolution therefore is:
#   1. GET $PbsMirror -> JSON array -> latest name matching ^\d+/$;
#   2. GET <PbsMirror>/<date>/ -> JSON array -> entry matching
#      ^cpython-<line>\.\d+\+ ... x86_64-pc-windows-msvc-
#      install_only\.tar\.gz$ (non-stripped variant);
#   3. real filename = masked name with the \*{2,} run replaced by the
#      date; download URL = <PbsMirror>/<date>/<real name> with '+'
#      percent-encoded as %2B (literal '+' also works; %2B is used);
#   4. expected sha256 from <PbsMirror>/<date>/SHA256SUMS -- manifest
#      lines carry the SAME masked filename, so we match on the masked
#      name (download source and checksum source are the same mirror
#      directory; verified live 2026-10-10, see header notes).
# ---------------------------------------------------------------------------

# GET a URL and return its text content, or $null on any failure.
# PS 5.1 returns a string for text responses; PS 7 sometimes hands
# back a raw byte[] -- decode those (SHA256SUMS / JSON listings are
# UTF-8/ASCII).
function Get-UrlText([string]$Url) {
    try {
        $c = (Invoke-WebRequest -UseBasicParsing -Uri $Url).Content
        if ($c -is [byte[]]) {
            return [Text.Encoding]::UTF8.GetString($c)
        }
        return "$c"
    } catch { return $null }
}

# Resolve the python-build-standalone archive from a npmmirror-style
# date-directory mirror. Returns a hashtable or $null:
#   @{ Date; MaskedName; Url; Size; Sha256 }
# $Sha256 is $null when the manifest could not be fetched/parsed; the
# CALLER decides whether that is fatal (default: it IS fatal, v5).
# v20: npmmirror keeps only the NEWEST patch of each minor in the
# LATEST date dir (live-checked 2026-10-13: 20261009/ carries
# 3.12.15, NO 3.12.10); pinned 3.12.10 lives in the OLD dir
# 20250409/ (identical masked-name layout + SHA256SUMS). v19 looked
# ONLY at the latest dir -> "could not resolve" -> silent downgrade
# to the registry-writing official installer. Now the date dirs are
# walked NEWEST-FIRST until the pinned version is found (bounded
# sweep, newest-first keeps "latest release" semantics when the
# version IS current).
function Get-PbsArchive([string]$Mirror, [string]$Version) {
    $root = $Mirror.TrimEnd('/') + '/'
    # 1) ALL date directories from the root JSON listing (v20: sorted
    # newest-first; v19 kept only the latest one)
    $rootJson = Get-UrlText $root
    if (-not $rootJson) { return $null }
    $dirs = @()
    try { $dirs = ($rootJson | ConvertFrom-Json) } catch { return $null }
    $dateDirs = @()
    foreach ($d in $dirs) {
        if ($d.name -match '^(\d{8})/$') { $dateDirs += $d.name }
    }
    if ($dateDirs.Count -eq 0) { return $null }
    # string sort == chronological for YYYYMMDD
    $dateDirs = @($dateDirs | Sort-Object -Descending)
    # bound the sweep: npmmirror date dirs are not frequent; the
    # whole list is < 100 entries, walking it is cheap, but stop at
    # the first dir that CONTAINS the pinned version.
    # v20 CRITICAL FIX: the v19 tail '\.\d+\+' only matches when
    # $Version is MAJOR.MINOR (it matches the PATCH segment, e.g.
    # "3.10" -> "cpython-3.10.15+..."). For a full x.y.z pin the
    # archive name is "cpython-<x.y.z>+<date>..." -- '+' DIRECTLY
    # after the patch, no extra ".NN". This is why -UsePbs NEVER
    # resolved 3.12.10 (independent of the date-dir issue). Make
    # the patch segment optional so both forms match.
    $escaped = '^cpython-' + $Version.Replace('.', '\.') + '(?:\.\d+)?\+'
    foreach ($latest in $dateDirs) {
        $date = $latest.TrimEnd('/')
        # 2) listing inside the date directory
        $dirJson = Get-UrlText ($root + $date + '/')
        if (-not $dirJson) { continue }
        $files = @()
        try { $files = ($dirJson | ConvertFrom-Json) } catch { continue }
        $want = $null
        foreach ($f in $files) {
            if ($f.name -match $escaped -and
                $f.name -match 'x86_64-pc-windows-msvc-install_only\.tar\.gz$') {
                $want = $f; break
            }
        }
        if (-not $want) { continue }
        # 3) real filename: replace the masked ** run with the date
        $realName = $want.name -replace '\*{2,}', $date
        $url = ($root + $date + '/' + [Uri]::EscapeDataString($realName))
        # 4) expected sha256 from the SAME directory's SHA256SUMS
        $sha = $null
        $sums = Get-UrlText ($root + $date + '/SHA256SUMS')
        if ($sums) {
            foreach ($line in ($sums -split "`n")) {
                $line = $line.Trim("`r")
                if ($line -like ('*' + $want.name)) {
                    $sha = ($line -split '\s+')[0]
                    break
                }
            }
        }
        return @{
            Date = $date; MaskedName = $want.name
            Url = $url; Size = [long]$want.size; Sha256 = $sha
        }
    }
    return $null
}

# v6: Get-LatestNodeVersion removed -- Node is pinned (v22.20.0) and
# never resolved dynamically. If you need latest again, npmmirror's
# index.tab is the source; it was deliberately dropped.

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            $bytes = $sha.ComputeHash($stream)
        } finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLower()
}

# Download with a bit of TLS hygiene for PS 5.1.
function Save-DownloadFile([string]$Url, [string]$Out) {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Out
}

# ---------------------------------------------------------------------------
# v5: hard download verification. A mirror can answer HTTP 200 with an
# HTML directory page where a tar.gz was expected (REAL incident on
# USTC, field report 2026-10-10: 262,161-byte text/html saved as
# .tar.gz, then handed to tar.exe -> "Unrecognized archive format" and
# a dead install). Every bootstrap download is now verified on THREE
# axes before it is allowed near an extractor:
#   1) Content-Type must match the expected type;
#   2) size must be inside the expected band (when $ExpectedSize is
#      known from the SAME listing that gave us the URL; HTML error
#      pages are 1-2 orders of magnitude off, but a small tolerance
#      accommodates mirror re-packing);
#   3) magic bytes must match ($Magic = hex string like "1F8B" for
#      gzip, "504B" for zip, "4D5A" for exe).
# Any mismatch -> $false; caller discards the file and retries/fails.
# $TypeName is used in error messages only.
# ---------------------------------------------------------------------------
function Test-DownloadedArchive(
    [string]$Path, [string]$TypeName, [string]$ContentType,
    [long]$ExpectedSize, [string]$MagicHex
) {
    $errors = @()
    # 1) magic bytes (strongest signal -- reads the actual file)
    if ($MagicHex) {
        $fs = $null; $br = $null
        try {
            $fs = [IO.File]::OpenRead($Path)
            $need = $MagicHex.Length / 2
            $buf = New-Object byte[] $need
            $got = $fs.Read($buf, 0, $need)
            if ($got -lt $need) { $errors += "file is smaller than ${need} bytes" }
            else {
                $actual = ([BitConverter]::ToString($buf)).Replace('-', '')
                if ($actual -ne $MagicHex) {
                    $errors += "magic bytes are $actual, expected $MagicHex (NOT a $TypeName)"
                }
            }
        } catch { $errors += "could not read file head: $($_.Exception.Message)" }
        finally {
            if ($br) { $br.Dispose() }
            if ($fs) { $fs.Dispose() }
        }
    }
    # 2) size band (only when the listing told us the exact size)
    if ($ExpectedSize -gt 0) {
        $len = (Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue).Length
        if ($len) {
            $lo = [long]($ExpectedSize * 0.5)
            $hi = [long]($ExpectedSize * 1.5)
            if ($len -lt $lo -or $len -gt $hi) {
                $errors += "size $len is far off the expected $ExpectedSize (listing said so)"
            }
        }
    }
    # 3) content type (from the response, when available)
    if ($ContentType) {
        if ($script:LastDownloadContentType -and
            $script:LastDownloadContentType -notmatch $ContentType) {
            $errors += "server Content-Type was '$($script:LastDownloadContentType)', expected /$ContentType/i"
        }
    }
    if ($errors.Count -gt 0) {
        Write-Warning "[install] download verification FAILED for ${Path}:"
        foreach ($e in $errors) { Write-Warning "          - $e" }
        return $false
    }
    return $true
}

# Download + verify. Returns $true on success. Fills
# $script:LastDownloadContentType so Test-DownloadedArchive can check
# it (PS 5.1 Invoke-WebRequest -OutFile gives no response object).
function Save-VerifiedDownload([string]$Url, [string]$Out, [string]$TypeName,
    [string]$ContentTypeRegex, [long]$ExpectedSize, [string]$MagicHex) {
    $script:LastDownloadContentType = ""
    try {
        [Net.ServicePointManager]::SecurityProtocol = `
            [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Out -PassThru
        if ($resp -and $resp.Headers) {
            $ct = $resp.Headers['Content-Type']
            if (-not $ct) { $ct = $resp.Headers['content-type'] }
            if ($ct) { $script:LastDownloadContentType = "$ct" }
        }
    } catch {
        Write-Warning "[install] download failed: $Url ($($_.Exception.Message))"
        return $false
    }
    if (-not (Test-Path -LiteralPath $Out)) {
        Write-Warning "[install] download produced no file: $Url"
        return $false
    }
    return (Test-DownloadedArchive -Path $Out -TypeName $TypeName `
        -ContentType $ContentTypeRegex -ExpectedSize $ExpectedSize -MagicHex $MagicHex)
}

# ---------------------------------------------------------------------------
# v8: interpreter discovery + decision.
# Discovery sources (BOUNDED, no whole-disk search):
#   - explicit -PythonPath
#   - py launcher `py -0p` (lists every registered interpreter at once)
#   - registry: HKCU:\Software\Python\PythonCore\*,
#     HKLM:\Software\Python\PythonCore\*,
#     HKLM:\Software\WOW6432Node\Python\PythonCore\* -- InstallPath
#     default value
#   - bounded common locations:
#       <drive>:\miniconda3|anaconda3|Miniconda3|Anaconda3\python.exe
#       (every FIXED drive, via Get-PSDrive)
#       %LOCALAPPDATA%\Programs\Python\Python3*\python.exe
#       %ProgramFiles%\Python3*\python.exe
#       C:\Python3*\python.exe
#   - python / python3 on PATH (WindowsApps Store stub filtered)
# Every candidate is ACTUALLY EXECUTED (-c "import sys; print(...)")
# before it is trusted; exes that do not run are dropped.
# Conda BASE environments are SKIPPED by default (shared installs,
# often 2GB+; CodeAgent must not tie its venv to a conda base).
# Selection order:
#   1. -PythonPath (explicit)
#   2. <InstallDir>\runtime\python\python.exe (pre-laid or bootstrapped)
#   3. <InstallDir>\python\.venv (a previous install's venv)
#   4. PATH interpreter, usable and >= 3.10
#   5. registry / common-location interpreter, usable and >= 3.10
#   6. none -> bootstrap the pinned official runtime
# -ForceOwnRuntime: skip 1/3/4/5 entirely and always end at 2
# (bootstrapping when needed).
# Returns a decision hashtable:
#   @{ Exe = path or $null; Reason = short text; Candidates = @(...) }
# where each candidate is
#   @{ Exe; Source; Version; Selected; SkipReason }
# ---------------------------------------------------------------------------
function Get-PythonVersionProbe([string]$Exe) {
    # Returns "X.Y" string or $null. Probe inside try/catch (v1
    # lesson: 2>$null redirect throws on PS 5.1 with EAP=Stop).
    $ver = $null
    try { $ver = (& $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])") } catch { }
    if ($ver) { return ([string]$ver).Trim() }
    return $null
}

function Test-PythonVersionOk([string]$Ver) {
    if (-not $Ver) { return $false }
    $parts = $Ver.Split('.')
    try {
        $major = [int]$parts[0]; $minor = [int]$parts[1]
    } catch { return $false }
    return (($major -gt 3) -or ($major -eq 3 -and $minor -ge 10))
}

function Test-WindowsAppsStub([string]$Exe) {
    return ($Exe -match '\\WindowsApps\\python(\d*)\.exe$')
}

function Test-CondaBase([string]$Exe) {
    # Conda BASE: <root>\python.exe directly under a
    # miniconda3/anaconda3 style root (an env would be
    # <root>\envs\<name>\python.exe). We refuse base by default.
    return ($Exe -match '\\(miniconda3|anaconda3|Miniconda3|Anaconda3)\\python\.exe$')
}

# Collect ALL discoverable interpreter candidates (dedup by path).
# Purely read-only; each exe is executed once for its version.
function Get-PythonCandidates([string]$Explicit, [string]$InstallDir) {
    $script:CandidateOrder = @()   # ordered list of paths
    $script:CandidateSource = @{}  # path -> source label
    function Add-Candidate([string]$Exe, [string]$Source) {
        if (-not $Exe) { return }
        $Exe = $Exe.ToLower()
        if (-not $script:CandidateSource.ContainsKey($Exe)) {
            $script:CandidateOrder += $Exe
            $script:CandidateSource[$Exe] = $Source
        }
    }

    # explicit
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { Add-Candidate $Explicit "-PythonPath" }
        else { throw "[install] -PythonPath not found: $Explicit" }
    }
    # own runtime + own venv (highest precedence after explicit)
    if ($InstallDir) {
        $runtimePy = Join-Path $InstallDir "runtime\python\python.exe"
        if (Test-Path -LiteralPath $runtimePy) { Add-Candidate $runtimePy "<InstallDir>\runtime\python" }
        $venvPy = Join-Path $InstallDir "python\.venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venvPy) { Add-Candidate $venvPy "<InstallDir>\python\.venv" }
    }
    # py launcher -0p (one shot, lists all registered interpreters)
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $listing = $null
        try { $listing = (& $pyCmd.Source -0p) } catch { }
        if ($listing) {
            foreach ($line in @($listing)) {
                # py -0p lines: " -3.12 *        C:\...\python.exe"
                if ($line -match '([A-Za-z]:\\[^\s].*python\.exe)\s*$') {
                    Add-Candidate $Matches[1] "py -0p"
                }
            }
        }
    }
    # PATH
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { Add-Candidate $cmd.Source "PATH ($name)" }
    }
    # registry (three roots; InstallPath default value)
    foreach ($regRoot in @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )) {
        if (-not (Test-PathSafe $regRoot)) { continue }
        # v13: plain drive-qualified subkey paths (no provider-qualified
        # $x.PSPath -- PS 5.1-safety and the v13 static gate).
        foreach ($tagPath in @((Get-RegistrySubKeys $regRoot) | Where-Object { $_ })) {
            $tagName = [string](Split-Path -Leaf $tagPath)
            # v14: InstallPath read (default OR named, non-empty wins,
            # trimmed) is centralized in Get-PythonCoreInstallPath.
            $ip = Get-PythonCoreInstallPath $tagPath
            if ($ip) {
                $exe = Join-Path $ip "python.exe"
                if (Test-Path -LiteralPath $exe) { Add-Candidate $exe "registry ($tagName)" }
            }
        }
    }
    # common locations (bounded; conda roots on every FIXED drive)
    $condaNames = @("miniconda3", "anaconda3", "Miniconda3", "Anaconda3")
    $fixedDrives = @()
    try {
        $fixedDrives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
            Where-Object { $_.Free -ne $null } | ForEach-Object { "$($_.Name):\" })
    } catch { }
    if (-not $fixedDrives -or $fixedDrives.Count -eq 0) { $fixedDrives = @("C:\") }
    foreach ($d in $fixedDrives) {
        foreach ($cn in $condaNames) {
            $cexe = Join-Path $d "$cn\python.exe"
            if (Test-Path -LiteralPath $cexe) { Add-Candidate $cexe "common location (conda root $d$cn)" }
        }
    }
    $programBases = @()
    if ($env:LOCALAPPDATA) { $programBases += (Join-Path $env:LOCALAPPDATA "Programs\Python") }
    if ($env:ProgramFiles) { $programBases += $env:ProgramFiles }
    foreach ($base in $programBases) {
        if ($base -and (Test-Path -LiteralPath $base)) {
            foreach ($d in (Get-ChildItem -LiteralPath $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue)) {
                $exe = Join-Path $d.FullName "python.exe"
                if (Test-Path -LiteralPath $exe) { Add-Candidate $exe "common location ($($d.FullName))" }
            }
        }
    }
    foreach ($d in @(Get-ChildItem "C:\" -Filter "Python3*" -ErrorAction SilentlyContinue | Where-Object { $_.PSIsContainer })) {
        $exe = Join-Path $d.FullName "python.exe"
        if (Test-Path -LiteralPath $exe) { Add-Candidate $exe "common location (C:\$($d.Name))" }
    }

    # probe every candidate (execute it) and build the result list
    $out = @()
    $seenCondaBase = $false
    foreach ($exe in $script:CandidateOrder) {
        $src = $script:CandidateSource[$exe]
        $skip = ""
        $ver = $null
        if (Test-WindowsAppsStub $exe) {
            $skip = "WindowsApps Store stub (does not run)"
        } else {
            $ver = Get-PythonVersionProbe $exe
            if (-not $ver) {
                $skip = "failed to execute"
            } elseif (-not (Test-PythonVersionOk $ver)) {
                $skip = "Python $ver is < 3.10"
            } elseif ($Explicit -and $exe -eq ([string]$Explicit).ToLower()) {
                # explicit choice overrides the conda-base refusal
            } elseif (Test-CondaBase $exe) {
                if ($AllowCondaBase) {
                    # v11: explicit opt-in; conda base is selectable.
                } else {
                    $skip = "conda BASE environment (shared install; to use it add -AllowCondaBase or -PythonPath)"
                    $seenCondaBase = $true
                }
            }
        }
        $out += @{
            Exe = $exe; Source = $src; Version = $ver
            Selected = $false; SkipReason = $skip
        }
    }
    # v15/v20: @() keeps this an ARRAY even with 0/1 elements AND
    # never wraps an empty array into a phantom element (the v19
    # comma-trick did exactly that -- see Get-RegistrySubKeys).
    return @($out)
}

# The DECISION. Applies the selection order over the candidates and
# returns @{ Exe; Reason; Candidates }.
function Find-Python([string]$Explicit, [string]$InstallDir) {
    $candidates = Get-PythonCandidates $Explicit $InstallDir

    if ($ForceOwnRuntime) {
        # v8: ignore everything pre-existing; caller bootstraps the
        # pinned runtime. (An explicit -PythonPath still wins -- the
        # user typed it.)
        if ($Explicit) {
            foreach ($c in $candidates) {
                if ($c.Exe -eq ([string]$Explicit).ToLower()) {
                    $c.Selected = $true
                    return @{ Exe = $c.Exe; Reason = "-PythonPath (explicit; overrides -ForceOwnRuntime)"; Candidates = $candidates }
                }
            }
        }
        $runtimePy = $(if ($InstallDir) { Join-Path $InstallDir "runtime\python\python.exe" } else { $null })
        $reason = "-ForceOwnRuntime: using the installer's own pinned runtime"
        if ($runtimePy -and (Test-Path -LiteralPath $runtimePy)) {
            $reason = "-ForceOwnRuntime: reusing the already-laid own runtime"
            return @{ Exe = $runtimePy; Reason = $reason; Candidates = $candidates }
        }
        # not laid yet -> return $null so the caller BOOTSTRAPS the
        # pinned runtime (a non-existent path must never be returned
        # as a decision -- the caller would try to venv with it).
        return @{ Exe = $null; Reason = $reason; Candidates = $candidates }
    }

    # selection by source precedence (candidates were added in order)
    foreach ($c in $candidates) {
        if ($c.SkipReason) { continue }
        $c.Selected = $true
        return @{ Exe = $c.Exe; Reason = "$($c.Source) ($($c.Version))"; Candidates = $candidates }
    }
    return @{ Exe = $null; Reason = "no usable Python found -- will bootstrap the pinned official $PythonVersion runtime"; Candidates = $candidates }
}

# ---------------------------------------------------------------------------
# v10: same-version official-installer registration check.
# The official python.org installer (a WiX bundle) is a PER-VERSION
# single instance: when this exact version is already REGISTERED
# (WixBundleInstalled=1), a silent /quiet run exits 0 WITHOUT
# installing anything and TargetDir is silently ignored. When the
# registration is DANGLING (registry says installed, files deleted),
# the Detect phase reports core/exe/dev/lib_JustForMe: Present and
# the SAME silent skip happens; /uninstall returns 1603 in that
# state, /repair (exit 0) then /uninstall cleans it up. So the
# registration MUST be checked BEFORE the installer is ever run.
# ---------------------------------------------------------------------------

# Major.minor key of a version string ("3.12.10" -> "3.12").
function Get-VersionKey([string]$Version) {
    if (-not $Version) { return "" }
    $p = ([string]$Version).Split('.')
    if ($p.Count -ge 2) { return "$($p[0]).$($p[1])" }
    return ([string]$Version)
}

# Registry roots to sweep (same three roots as Get-PythonCandidates
# plus the ARP uninstall keys and the WiX Dependencies tree, which
# are the surfaces the official installer actually writes).
# Read-only. Returns @{ Found; Exe; Sources } where Sources lists
# every registry surface that mentioned this version.
function Get-RegisteredPythonInstall([string]$Version) {
    $key = Get-VersionKey $Version
    $exact = [string]$Version
    $out = @{ Found = $false; Exe = $null; Sources = @() }
    if (-not $key) { return $out }

    # 1) PythonCore\<tag>\InstallPath (default value)
    #    v13: plain subkey paths via helpers (no $x.PSPath).
    $tagDirs = @(Get-RegistrySubKeys "HKCU:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\WOW6432Node\Python\PythonCore")
    foreach ($tagPath in @($tagDirs | Where-Object { $_ })) {
        $tagName = [string](Split-Path -Leaf $tagPath)
        if ((Get-VersionKey $tagName) -ne $key) { continue }
        # v14: InstallPath read (default OR named, non-empty wins,
        # trimmed) is centralized in Get-PythonCoreInstallPath.
        $ip = Get-PythonCoreInstallPath $tagPath
        $out.Found = $true
        if ($ip) {
            $out.Exe = Join-Path $ip "python.exe"
            $out.Sources += "PythonCore $tagName (InstallPath: $ip)"
        } else {
            $out.Sources += "PythonCore $tagName (InstallPath value missing)"
        }
    }

    # 2) ARP uninstall entries (DisplayName contains the exact
    #    version, e.g. "Python 3.12.10"). Both HKCU and HKLM views.
    $arpDirs = @(Get-RegistrySubKeys "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
    $arpDirs += @(Get-RegistrySubKeys "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
    $arpDirs += @(Get-RegistrySubKeys "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    foreach ($ePath in $arpDirs) {
        $props = Get-RegistryKeyValues $ePath
        if (-not $props) { continue }
        $display = $props.DisplayName
        if (-not $display) { continue }
        if (([string]$display) -notmatch 'Python') { continue }
        if (([string]$display) -notmatch [regex]::Escape($exact)) { continue }
        $loc = $props.InstallLocation
        $out.Found = $true
        if ($loc) {
            $exe = Join-Path $loc "python.exe"
            if (-not $out.Exe) { $out.Exe = $exe }
            $out.Sources += "ARP $ePath (DisplayName '$display', InstallLocation: $loc)"
        } else {
            $out.Sources += "ARP $ePath (DisplayName '$display', no InstallLocation)"
        }
    }

    # 3) HKCU\Software\Classes\Installer\Dependencies: the WiX
    #    dependency keys referenced by the bundle. We flag ANY
    #    dependency whose DisplayName mentions this exact Python
    #    version (that is what makes /uninstall 1603 while files
    #    are missing). Deeper MSI key comparison is out of scope;
    #    this is a tripwire, not a full MSI inventory.
    $depDirs = @(Get-RegistrySubKeys "HKCU:\Software\Classes\Installer\Dependencies")
    foreach ($depPath in $depDirs) {
        $props = Get-RegistryKeyValues $depPath
        if (-not $props) { continue }
        $display = $props.DisplayName
        if (-not $display) { continue }
        if (([string]$display) -match 'Python' -and ([string]$display) -match [regex]::Escape($exact)) {
            $out.Found = $true
            $out.Sources += "WiX dependency $depPath (DisplayName '$display')"
        }
    }
    return $out
}

# Decide what to do with the pinned-version registration before the
# official installer is invoked. Returns @{ Action =
#   "reuse"     -- registration healthy, Exe is a working pinned-version
#                  interpreter; the caller builds the venv on it, no
#                  download, no installer run;
#   "clear"     -- no registration found; safe to run the installer;
#   "broken"    -- registration exists but the interpreter is
#                  missing/broken; the caller must HARD-STOP with the
#                  repair recipe (never a silent continue);
#   "conflict"  -- registration exists AND -ForceOwnRuntime was given:
#                  a second same-version copy is impossible.
#   Exe; Sources }.
# v18: is there a VERIFIED local Python payload the user explicitly
# supplied (-PythonArchive) or that is already cached in the package
# dir? When yes, that payload WINS over any registry-reuse: the user
# handing us a self-contained archive means "install THIS, here" --
# the previous REUSE-first priority produced installs whose venv
# silently depended on a foreign directory (field: stageE offline
# install reused D:\ca-v3-d, so D:\ca-v3-e\runtime\python never
# existed and the venv died when ca-v3-d was deleted days later).
# NOTE: a .exe archive is only "verified" here AFTER
# Install-PythonRuntime's Authenticode/size checks -- for the reuse
# gate we treat an existing + readable .exe as archive-intent (the
# installer path re-verifies before running); a tar.gz additionally
# needs gzip magic (cheap 2-byte check, same as the real gate).
function Test-LocalPythonArchiveIntent {
    if ($PythonArchive -and (Test-Path -LiteralPath $PythonArchive)) {
        $ext = [IO.Path]::GetExtension($PythonArchive).ToLower()
        if ($ext -eq ".exe") { return @{ Intent = $true; Path = $PythonArchive } }
        try {
            $fs = [IO.File]::OpenRead($PythonArchive)
            try {
                $b = New-Object byte[] 2
                if ($fs.Read($b, 0, 2) -eq 2 -and $b[0] -eq 0x1F -and $b[1] -eq 0x8B) {
                    return @{ Intent = $true; Path = $PythonArchive }
                }
            } finally { $fs.Dispose() }
        } catch { }
        return @{ Intent = $false; Path = $PythonArchive }
    }
    return @{ Intent = $false; Path = "" }
}

function Test-PinnedPythonRegistration([string]$Version, [bool]$Force) {
    # v20: -IgnoreRegistryState -- treat the registration state as
    # absent everywhere (no reuse, no broken gate, no conflict).
    if ($IgnoreRegistryState) {
        return @{ Action = "ignored"; Exe = $null; Sources = @() }
    }
    $reg = Get-RegisteredPythonInstall $Version
    $res = @{ Action = "clear"; Exe = $null; Sources = $reg.Sources }
    if (-not $reg.Found) { return $res }

    # registration exists: is its interpreter alive AND the pinned
    # exact version? Run it (same probe discipline as v8).
    $ver = $null
    $exeOk = $false
    if ($reg.Exe -and (Test-Path -LiteralPath $reg.Exe)) {
        $ver = $null
        try { $ver = (& $reg.Exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])") } catch { }
        if ($ver -and (([string]$ver).Trim() -eq [string]$Version)) { $exeOk = $true }
    }
    if ($exeOk) {
        # v16: -ForceOwnRuntime + a HEALTHY same-version registration is
        # ALSO a conflict (field: v15 silently reused it and the second
        # install "succeeded" with exit 0). The official installer
        # cannot lay a second same-version copy; a healthy one is not
        # less of an obstacle than a dangling one.
        if ($Force) {
            $res.Action = "conflict"
            $res.Exe = $null
            return $res
        }
        $res.Action = "reuse"
        $res.Exe = $reg.Exe
        return $res
    }
    if ($Force) {
        $res.Action = "conflict"
        return $res
    }
    $res.Action = "broken"
    return $res
}

# v10: dump the official installer log's three diagnostic line
# families: WixBundleInstalled, Detect-phase package states, and
# the final Exit code. Those three lines are exactly what located
# this task's root cause. Best-effort; never throws.
function Show-PythonInstallerLog([string]$LogPath) {
    if (-not $LogPath -or -not (Test-Path -LiteralPath $LogPath)) {
        Write-Warning "[install] (no installer log found at $LogPath)"
        return
    }
    $lines = @()
    try { $lines = @(Get-Content -LiteralPath $LogPath -ErrorAction SilentlyContinue) } catch { }
    if (-not $lines -or $lines.Count -eq 0) {
        Write-Warning "[install] (installer log is empty or unreadable: $LogPath)"
        return
    }
    Write-Host "[install] ---- python-installer.log diagnostics ($LogPath) ----"
    foreach ($ln in $lines) {
        if ($ln -match 'WixBundleInstalled') {
            Write-Host "[install]   LOG: $ln"
        }
    }
    foreach ($ln in $lines) {
        # WiX package-state lines look like:
        #   i052: Package 'core_JustForMe', state: Present (local)
        if ($ln -match 'Package .*state: (Present|Absent)' -or $ln -match 'Detect .*(begin|complete)') {
            Write-Host "[install]   LOG: $ln"
        }
    }
    foreach ($ln in $lines) {
        if ($ln -match 'Exit code:') {
            Write-Host "[install]   LOG: $ln"
        }
    }
    Write-Host "[install] ---- end installer log diagnostics ----"
}

# ---------------------------------------------------------------------------
# v11: uninstall-side registration cleanup. When OUR install laid the
# pinned official Python (TargetDir under <InstallDir>\runtime\python),
# the official installer's OWN WiX registration (ARP entry + 8
# Installer\Dependencies keys) must go too -- otherwise the next
# same-version install SILENTLY SKIPS (WixBundleInstalled=1). The
# removal is delegated to the official installer itself:
# /uninstall /quiet, preceded by /repair /quiet when a direct
# /uninstall fails (field-verified: files-deleted state -> direct
# /uninstall returns 1603; /repair restores consistency, then
# /uninstall succeeds).
# ---------------------------------------------------------------------------

# Resolve the official installer exe for the pinned Python version.
# Package-dir cache FIRST (the 4-axis check: size band + MZ magic +
# Authenticode PSF; content-type only applies to downloads). Else
# download from the same mirrors as the install path. Returns the
# verified exe path, or throws.
function Get-PythonInstallerExe([string]$DlDir) {
    $pkgName = Get-PackageFileName "PythonInstaller"
    $candidate = Join-Path $PackageDir $pkgName
    $archive = $null
    $usedLabel = "cached in package dir"
    if ((Test-Path -LiteralPath $PackageDir) -and
        (Test-LocalPackage -Path $candidate -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A")) {
        $archive = $candidate
        Write-Host "[uninstall] using cached python installer from package dir: $candidate (no download)"
    } else {
        $sources = @(
            @{ Label = "Tsinghua"; Url = ($PythonInstallerMirror.TrimEnd('/') + "/$PythonVersion/$pkgName") }
            @{ Label = "Aliyun";   Url = ($PythonInstallerMirror2.TrimEnd('/') + "/$pkgName") }
        )
        $usedLabel = ""
        foreach ($src in $sources) {
            $outPath = Join-Path $DlDir $pkgName
            Write-Host "[uninstall] official installer source $($src.Label): downloading $($src.Url)"
            if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force -ErrorAction SilentlyContinue }
            if (Save-VerifiedDownload -Url $src.Url -Out $outPath `
                -TypeName "official python installer exe" -ContentTypeRegex 'application/(octet-stream|x-ms-dos-executable|msdownload|x-msdownload)' `
                -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                $usedLabel = $src.Label; $archive = $outPath; break
            }
            Write-Warning "[uninstall] $($src.Label) failed -- trying the next source"
        }
        if (-not $archive) {
            throw "[uninstall] could not obtain python-$PythonVersion-amd64.exe (package dir '$PackageDir' has no verified copy and both mirrors failed). The official installer registration was NOT cleaned -- remove it manually (see the recipe below)."
        }
    }
    # Authenticode: identical rule to the install path.
    $sig = Get-AuthenticodeSignature -FilePath $archive
    if ($sig.Status -ne 'Valid') {
        throw "[uninstall] python installer Authenticode signature is '$($sig.Status)' (expected Valid). REFUSING to run it. File: $archive (source: $usedLabel)"
    }
    $signer = ""
    if ($sig.SignerCertificate -and $sig.SignerCertificate.Subject) { $signer = $sig.SignerCertificate.Subject }
    if ($signer -notmatch 'Python Software Foundation') {
        throw "[uninstall] python installer is signed by '$signer', expected 'Python Software Foundation'. REFUSING to run it. File: $archive (source: $usedLabel)"
    }
    Write-Host "[uninstall] Authenticode OK: $signer ($($sig.Status), source: $usedLabel)"
    return $archive
}

# v14: read a PythonCore <tag>\InstallPath value ONCE, from ONE
# place. The official installer writes the path as the (default)
# value with a TRAILING BACKSLASH and leaves the NAMED InstallPath
# value EMPTY (field fact from a real machine: (default) = "D:\...\python\",
# InstallPath = ""). v13's per-sweep "(default) then named" reads
# plus raw string compares were spread over three functions; now
# every sweep calls this. $TagPath = full tag key path; returns the
# non-empty value (already Trim()'d) or $null.
function Get-PythonCoreInstallPath([string]$TagPath) {
    if (-not $TagPath) { return $null }
    $props = Get-RegistryKeyValues (Join-Path $TagPath "InstallPath")
    if (-not $props) { return $null }
    # (default) first (what the official installer actually writes),
    # then the named InstallPath (some tools write it instead).
    # Whichever is NON-EMPTY wins; trimmed of outer whitespace.
    $ip = $props.'(default)'
    if (-not $ip) { $ip = $props.InstallPath }
    if (-not $ip) { return $null }
    return (([string]$ip).Trim())
}

# v14: normalize a path for comparison. Trim outer whitespace,
# strip trailing separators, unify separators, and (on Windows)
# resolve with [IO.Path]::GetFullPath so relative / mixed-separator
# / trailing-slash variants all land on the same string. Never
# throws: on GetFullPath failure (e.g. an odd registry value) the
# separator-normalized form is returned as-is.
function Convert-PathForCompare([string]$P) {
    if (-not $P) { return "" }
    $p = $P.Trim()
    if (-not $p) { return "" }
    $p = $p.Replace('/', '\')
    $p = $p.TrimEnd('\')
    try { $p = [IO.Path]::GetFullPath($p) } catch { }
    return $p
}

# v11: Does path $P live inside directory $Root? v14 rewrite: both
# sides go through Convert-PathForCompare (trim + trailing-separator
# strip + separator unification + GetFullPath on Windows), then a
# case-insensitive "prefix + separator" test (equal counts as
# inside). The the field box field case -- (default) value with a trailing
# backslash -- now matches. $P may be $null/"" -> $false.
function Test-PathUnder([string]$P, [string]$Root) {
    if (-not $P -or -not $Root) { return $false }
    $p = Convert-PathForCompare $P
    $r = Convert-PathForCompare $Root
    if (-not $p -or -not $r) { return $false }
    if ($p -eq $r) { return $true }
    return $p.StartsWith($r + '\', [StringComparison]::OrdinalIgnoreCase)
}

# v13: Test-Path for a possibly-EMPTY variable path. Windows
# PowerShell 5.1 THROWS "LiteralPath parameter cannot bind ...
# empty string" (field-reproduced; pwsh 7 returns False -- the
# Linux harness cannot see it). Every Test-Path -LiteralPath whose
# argument is a variable (and therefore possibly $null/"") MUST go
# through this helper.
function Test-PathSafe([string]$Path) {
    if (-not $Path) { return $false }
    return (Test-Path -LiteralPath $Path)
}

# ---------------------------------------------------------------------------
# v17: DATA-DIR resolution + ownership (the -PurgeData field incident:
# the uninstall side hardcoded %APPDATA%\CodeAgent and ignored the
# -DataDir the install side had actually used, then went and deleted
# the DEFAULT dir twice while the real target sat untouched).
# Resolution is now ONE rule set shared by install and uninstall:
#     explicit -DataDir  >  install-time record  >  Electron default
# and purge is gated by an ownership PROOF (marker written at install
# time inside the data dir, compared against the InstallDir at purge
# time). No proof -> refuse to purge, print an explicit hint.
# ---------------------------------------------------------------------------

# Marker file we drop INSIDE the data dir at install time. Records the
# install dir the data dir belongs to, so a later -PurgeData can prove
# "this dir belongs to THAT install" before deleting anything.
$Script:DataOwnershipFile = ".codeagent-install"
# Marker inside the InstallDir (.install-state.json) also carries the
# resolved dataDir (belt and suspenders: the install dir record is
# readable even when the data dir is on a drive the marker did not
# reach, e.g. a hand-copied data tree).

# The one shared resolution rule. Returns a hashtable:
#   @{ DataDir = <resolved path>; Source = "explicit"|"record"|"default" }
function Resolve-DataDir([string]$ExplicitDataDir, [string]$InstallDirForRecord) {
    # 1) explicit -DataDir always wins (both sides of the lifecycle)
    if ($ExplicitDataDir -and $ExplicitDataDir.Trim()) {
        $d = [Environment]::ExpandEnvironmentVariables($ExplicitDataDir.Trim())
        return @{ DataDir = $d; Source = "explicit" }
    }
    # 2) install-time record in the install dir (.install-state.json)
    if ($InstallDirForRecord -and (Test-PathSafe (Join-Path $InstallDirForRecord ".install-state.json"))) {
        try {
            $rec = Get-Content -LiteralPath (Join-Path $InstallDirForRecord ".install-state.json") -Raw |
                ConvertFrom-Json -ErrorAction Stop
            $recData = [string]$rec.dataDir
            if ($recData.Trim()) {
                return @{ DataDir = [Environment]::ExpandEnvironmentVariables($recData.Trim()); Source = "record" }
            }
        } catch { }
    }
    # 3) Electron default
    return @{ DataDir = (Join-Path $env:APPDATA $Script:AppDirName); Source = "default" }
}

# v22 (t_a8ceb810, the "-PurgeData forged-stamp" risk): write the
# marker ONLY when THIS install genuinely owns the data dir, i.e. the
# dir did not pre-exist with a DIFFERENT install's stamp in it, and
# one of:
#   (a) -DataDir was explicitly given (user declared it), or
#   (b) the data dir lives INSIDE the InstallDir, or
#   (c) the dir already carries a stamp naming THIS InstallDir
#       (a prior install from the same place -- same owner).
# Otherwise (e.g. install to D:\ elsewhere while data resolves to the
# Electron default %APPDATA%\CodeAgent) the dir is read-only for us:
# stamp NOTHING -- a stale/absent marker means purge later REFUSES,
# which is the safe direction. If the dir exists and carries a stamp
# for a DIFFERENT install dir, keep it, claim nothing, warn loudly.
function Write-DataOwnershipMarkerIfOwned([string]$DataDir, [string]$InstallDir) {
    if (-not $DataDir -or -not $InstallDir) { return }
    if (-not (Test-PathSafe $DataDir)) {
        # Dir does not exist yet: Electron will create it later, and it
        # will NOT be under this install's control unless -DataDir said
        # so or it is inside the InstallDir. Marking a not-yet-existing
        # default dir would exactly reproduce the reported incident
        # (a stamp lands in %APPDATA%\CodeAgent for an install whose
        # -InstallDir is elsewhere) -- so only pre-declare ownership
        # when this install owns it by construction.
        $owns = $false
        if ($Script:DataDirExplicit) { $owns = $true }
        elseif ((Test-PathUnder -P $DataDir -Root $InstallDir)) { $owns = $true }
        if ($owns) {
            Write-DataOwnershipMarker $DataDir $InstallDir
        } else {
            Write-Host "[install] data dir '$DataDir' does not exist yet and is not owned by this install -- no ownership marker pre-declared (a later -PurgeData will refuse to touch it unless it can prove ownership)"
        }
        return
    }
    # Dir exists: inspect any stamp already inside it.
    $markerPath = Join-Path $DataDir $Script:DataOwnershipFile
    $existingInstall = $null
    if (Test-PathSafe $markerPath) {
        try {
            $m = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json -ErrorAction Stop
            $existingInstall = [string]$m.installDir
        } catch {
            Write-Warning "[install] data dir $DataDir contains an unreadable ownership marker -- treating it as NOT ours, refusing to overwrite"
            return
        }
    }
    if ($existingInstall -and $existingInstall.Trim() -and
        ((Convert-PathForCompare $existingInstall) -ne (Convert-PathForCompare $InstallDir))) {
        # 3) someone else's stamp: do not overwrite, do not claim, warn
        Write-Warning ("[install] data dir '{0}' is already marked as belonging to a DIFFERENT install '{1}' -- marker left untouched; this install did NOT claim the directory (a later -PurgeData from THIS install will refuse it)" -f $DataDir, $existingInstall)
        return
    }
    # No stamp, or a stamp that already names this install dir.
    # Claim it only when this install genuinely owns it (explicit
    # -DataDir, inside the InstallDir, or a matching prior stamp).
    $owns = $false
    if ($Script:DataDirExplicit) { $owns = $true }
    elseif ((Test-PathUnder -P $DataDir -Root $InstallDir)) { $owns = $true }
    elseif ($existingInstall -and $existingInstall.Trim()) { $owns = $true }
    if ($owns) {
        Write-DataOwnershipMarker $DataDir $InstallDir
    } else {
        Write-Host "[install] data dir '$DataDir' exists but is NOT owned by this install (no -DataDir, not inside '$InstallDir', no matching prior marker) -- left read-only for the installer: no ownership marker written"
    }
}

# Write the ownership marker into the data dir (best effort -- a
# read-only or not-yet-created data dir must never fail an install:
# purge then simply demands an explicit -DataDir later, which is the
# safe direction). Called ONLY from Write-DataOwnershipMarkerIfOwned
# after ownership has been proven -- never call directly.
function Write-DataOwnershipMarker([string]$DataDir, [string]$InstallDir) {
    if (-not $DataDir -or -not $InstallDir) { return }
    if (-not (Test-PathSafe $DataDir)) { return }   # created by Electron later; no marker possible
    try {
        @{ app = $Script:AppDirName; installDir = $InstallDir; markedAt = (Get-Date).ToString("s") } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $DataDir $Script:DataOwnershipFile) -Encoding UTF8 -Force
    } catch {
        Write-Warning "[install] could not write data-dir ownership marker into $DataDir ($($_.Exception.Message)) -- -PurgeData will require an explicit -DataDir"
    }
}

# PROOF gate. $Candidate is the dir -PurgeData is about to delete.
# v22 (t_a8ceb810): the ONLY sufficient proof is the ownership marker
# INSIDE the candidate naming this InstallDir (a). The old rules
#   b) candidate == dataDir recorded in .install-state.json
#   c) candidate is the Electron default AND the record says default
# are DEMOTED to necessary conditions -- they may no longer authorize
# a purge on their own, because the install-time record says "this
# install USED that dir", not "we own the WHOLE dir and everything
# the user put in it". Concretely: installing with -InstallDir D:\...
# used to (unconditionally) stamp the default %APPDATA%\CodeAgent,
# so an uninstall -PurgeData could prove ownership of the USER'S REAL
# DATA DIR via that forged stamp -- the exact mis-delete setup this
# task is about. Since v22 the install side only stamps dirs it
# genuinely owns; a dir with no marker (or a marker naming another
# install) is refused, whatever the record says.
function Test-DataDirBelongsToInstall([string]$Candidate, [string]$InstallDir, [string]$RecordedDataDir) {
    if (-not $Candidate) { return $false }
    $c = Convert-PathForCompare $Candidate
    # a) marker inside the candidate names this install dir
    $markerPath = Join-Path $Candidate $Script:DataOwnershipFile
    if (Test-PathSafe $markerPath) {
        try {
            $m = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json -ErrorAction Stop
            $mInstall = [string]$m.installDir
            if ($mInstall.Trim() -and
                ((Convert-PathForCompare $mInstall) -eq (Convert-PathForCompare $InstallDir))) {
                return $true
            }
        } catch { }
        # a) marker exists but is unreadable or names another install:
        # explicitly NOT ours -- do not fall through to the record.
        return $false
    }
    # no marker inside: if the dir exists, we cannot prove we own the
    # user data in it -> refuse. (v22: record/default match alone is
    # no longer proof.)
    if (Test-PathSafe $Candidate) { return $false }
    # dir does not exist: nothing to delete; caller treats absent dirs
    # separately, but a recorded match at least confirms we are pointed
    # at the right (nonexistent) target.
    if ($RecordedDataDir -and ((Convert-PathForCompare $RecordedDataDir) -eq $c)) { return $true }
    return $false
}

# Uninstall-side purge decision: resolves the target with the shared
# rules, proves ownership, and either purges or REFUSES (with the
# explicit hint the task demands). Prints everything it decided.
# Returns $true when the dir is gone-or-was-never-there (not a
# failure), $false when it must count as INCOMPLETE.
function Invoke-PurgeData([string]$ExplicitDataDir, [string]$InstallDir, [bool]$Force) {
    $res = Resolve-DataDir $ExplicitDataDir $InstallDir
    $target = $res.DataDir
    Write-Host ("[uninstall] -PurgeData: data dir resolved as {0} (source: {1})" -f $target, $res.Source)
    # read the install-time record for ownership comparison (v17:
    # snapshotted BEFORE the install dir was deleted; fall back to
    # reading it live in case the dir still exists)
    $recorded = [string]$Script:UninstallRecordedDataDir
    $recPath = Join-Path $InstallDir ".install-state.json"
    if ((-not $recorded.Trim()) -and (Test-PathSafe $recPath)) {
        try {
            $rec = Get-Content -LiteralPath $recPath -Raw | ConvertFrom-Json -ErrorAction Stop
            $recorded = [string]$rec.dataDir
        } catch { }
    }
    if (-not (Test-PathSafe $target)) {
        Write-Host "[uninstall] data dir not present: $target (nothing to purge)"
        return $true
    }
    if (-not (Test-DataDirBelongsToInstall $target $InstallDir $recorded)) {
        # unproven: refuse unless the user passed -DataDir explicitly
        # AND confirmed via -Force... NO: the task is explicit --
        # cannot prove -> refuse, period. Explicit -DataDir alone is
        # NOT proof (that is how the earlier data-directory incident deleted the wrong
        # dir). The install marker is the only proof.
        Write-Warning @"
[uninstall] REFUSED to purge data dir: $target
  Reason: cannot prove this directory belongs to the install at $InstallDir
  (no ownership marker inside it, and the install record does not match).
  If you really want it gone, delete it manually -- this script will not.
"@
        return $false
    }
    Write-Host "[uninstall] -PurgeData: removing data dir $target (ownership verified)"
    $purged = Remove-DirectoryClean $target
    if (-not $purged) {
        Write-Warning "[uninstall] INCOMPLETE: data dir $target could NOT be fully deleted (see warnings above). Close any app still holding files there and re-run: -Uninstall -PurgeData"
        return $false
    }
    Write-Host "[uninstall] data dir removed (verified gone)"
    return $true
}

# v13: read one registry subkey path ("HKCU:\...\PythonCore\3.12",
# a PLAIN drive-qualified path built with Join-Path -- never a
# provider-qualified "Registry::HKEY_..." string) and return a
# hashtable of its values. Returns $null when the key does not
# exist or cannot be read. PS 5.1-safe: no -LiteralPath, no
# $x.PSPath, no wildcard characters in the literal tail (registry
# tag names like "3.12" contain none).
function Get-RegistryKeyValues([string]$KeyPath) {
    if (-not $KeyPath) { return $null }
    try {
        $item = Get-Item -Path $KeyPath -ErrorAction SilentlyContinue
        if (-not $item) { return $null }
        $props = Get-ItemProperty -Path $KeyPath -ErrorAction SilentlyContinue
        if (-not $props) { return $null }
        return $props
    } catch { return $null }
}

# v13: enumerate the SUBKEY NAMES of a registry root path
# ("HKCU:\Software\Python\PythonCore"). Returns an array of
# full subkey paths (root + "\" + name), empty when the root is
# missing/empty/unreadable. Plain -Path semantics throughout.
# v20 CRITICAL FIX: `return ,$out` with $out=@() returned a
# ONE-ELEMENT ARRAY CONTAINING AN EMPTY ARRAY -- callers' foreach
# then iterated that empty-array ELEMENT (not zero elements) and
# Split-Path -Leaf crashed with
# ParameterArgumentValidationErrorEmptyArrayNotAllowed (field:
# the field box empty HKCU PythonCore root, seconds into install; repro on
# pwsh 7.4.6). The comma-trick was added in v13 for the one-element
# unwrapping bug; applied to an EMPTY array it poisons every caller.
# New contract: only real, non-blank STRING paths are appended
# (an unrolled empty/blank item can never enter $out), and the
# return value stays a real array thanks to @() of the pipeline.
function Get-RegistrySubKeys([string]$RootPath) {
    if (-not $RootPath) { return @() }
    if (-not (Test-Path -LiteralPath $RootPath)) { return @() }
    # v20: collect ONLY string, non-blank entries. Any pipeline stage
    # that unrolls to $null / an empty array / a blank string is
    # dropped here -- the @() wrapper keeps the result a true ARRAY
    # (0 or N elements, never an empty-array ELEMENT).
    $items = @()
    foreach ($k in (Get-ChildItem -LiteralPath $RootPath -ErrorAction SilentlyContinue)) {
        if ($k -and $k.PSChildName) {
            $p = (Join-Path $RootPath ([string]$k.PSChildName))
            if ($p -and ([string]$p).Trim()) { $items += [string]$p }
        }
    }
    return @($items | Where-Object { $_ -is [string] -and $_.Trim() })
}

# v13: Is the pinned-version registration OURS? The official
# per-user installer NEVER writes an ARP InstallLocation (field
# fact from a real machine), so ownership is decided ONLY by a PythonCore
# <tag>\InstallPath pointing INSIDE <InstallDir>. When ours, the
# official installer chain removes PythonCore + ARP + WiX
# Dependencies together. When not ours (someone else's same
# version -- e.g. InstallPath elsewhere or absent), we touch
# NOTHING and the installer is never run against it.
# Returns @{ Ours; Report }.
function Test-RegistrationIsOurs([string]$Version, [string]$InstallDir) {
    $res = @{ Ours = $false; Report = @() }
    $key = Get-VersionKey $Version
    if (-not $key) { return $res }
    $roots = @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )
    $tagDirs = @(Get-RegistrySubKeys "HKCU:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\WOW6432Node\Python\PythonCore")
    foreach ($tagPath in @($tagDirs | Where-Object { $_ })) {
        $name = [string](Split-Path -Leaf $tagPath)
        if ((Get-VersionKey $name) -ne $key) { continue }
        # v14: InstallPath read (default OR named, non-empty wins,
        # trimmed) is centralized in Get-PythonCoreInstallPath; the
        # comparison itself is the normalized Test-PathUnder below.
        $ip = Get-PythonCoreInstallPath $tagPath
        if (-not $ip) {
            $res.Ours = $true
            $res.Report += "PythonCore $name (InstallPath value missing) -- treated as ours from this install (path unknown)"
            continue
        }
        if (Test-PathUnder $ip $InstallDir) {
            $res.Ours = $true
            $res.Report += "PythonCore $name (InstallPath: $ip) -- OURS (under $InstallDir): the official installer will be run to clean PythonCore + ARP + WiX Dependencies"
        } else {
            $res.Report += "PythonCore $name (InstallPath: $ip) -- NOT ours (someone else's $Version); the installer will NOT be run"
        }
    }
    return $res
}

# Run the official installer to remove OUR pinned-version
# registration. Field-verified sequence: direct /uninstall first; if
# it fails (files-deleted state -> 1603), /repair /quiet (restores
# file consistency, exit 0) then /uninstall again. $Exe = verified
# installer path. Logs always land in $LogDir. Returns $true on
# success (final /uninstall rc == 0). Best-effort on logging only.
function Remove-OfficialPythonRegistration([string]$Exe, [string]$LogDir) {
    $unLog = Join-Path $LogDir "python-uninstall.log"
    $repLog = Join-Path $LogDir "python-repair.log"
    Write-Host "[uninstall] running the official installer: /uninstall /quiet (log: $unLog)"
    & $Exe /uninstall /quiet "/log=$unLog" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[uninstall] official installer /uninstall: exit 0"
        return $true
    }
    Write-Warning "[uninstall] direct /uninstall failed (rc=$LASTEXITCODE) -- likely the files-were-deleted state; running /repair /quiet first (log: $repLog)"
    & $Exe /repair /quiet "/log=$repLog" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[uninstall] /repair failed too (rc=$LASTEXITCODE) -- the registration was NOT removed by the installer"
        return $false
    }
    Write-Host "[uninstall] /repair: exit 0 -- file consistency restored, uninstalling again (log: $unLog)"
    & $Exe /uninstall /quiet "/log=$unLog" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[uninstall] official installer /uninstall (after /repair): exit 0"
        return $true
    }
    Write-Warning "[uninstall] /uninstall after /repair still failed (rc=$LASTEXITCODE)"
    return $false
}

# Post-uninstall verification for the PINNED version's registration.
# Returns @{ Total = <count of all three surfaces>; PythonCore; ARP;
# Deps } -- Total must be 0 for a clean uninstall. Read-only.
# v13: reads go through Get-RegistrySubKeys/Get-RegistryKeyValues
# (plain paths, PS 5.1-safe).
function Get-RegistrationResidue([string]$Version) {
    $res = @{ Total = 0; PythonCore = 0; ARP = 0; Deps = 0 }
    $key = Get-VersionKey $Version
    $exact = [string]$Version
    if (-not $key) { return $res }
    $tagDirs = @(Get-RegistrySubKeys "HKCU:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\Python\PythonCore")
    $tagDirs += @(Get-RegistrySubKeys "HKLM:\Software\WOW6432Node\Python\PythonCore")
    foreach ($tagPath in @($tagDirs | Where-Object { $_ })) {
        $name = [string](Split-Path -Leaf $tagPath)
        if ((Get-VersionKey $name) -eq $key) { $res.PythonCore++ }
    }
    $arpDirs = @(Get-RegistrySubKeys "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
    $arpDirs += @(Get-RegistrySubKeys "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
    $arpDirs += @(Get-RegistrySubKeys "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    foreach ($ePath in $arpDirs) {
        $props = Get-RegistryKeyValues $ePath
        if (-not $props) { continue }
        $display = $props.DisplayName
        if (-not $display) { continue }
        if (([string]$display) -match 'Python' -and ([string]$display) -match [regex]::Escape($exact)) { $res.ARP++ }
    }
    $depDirs = @(Get-RegistrySubKeys "HKCU:\Software\Classes\Installer\Dependencies")
    foreach ($depPath in $depDirs) {
        $props = Get-RegistryKeyValues $depPath
        if (-not $props) { continue }
        $display = $props.DisplayName
        if (-not $display) { continue }
        if (([string]$display) -match 'Python' -and ([string]$display) -match [regex]::Escape($exact)) { $res.Deps++ }
    }
    $res.Total = $res.PythonCore + $res.ARP + $res.Deps
    return $res
}


# ---------------------------------------------------------------------------
# Phase 0: environment check (v3 -- NOT field-tested on a real Windows box)
# All probes are strictly READ-ONLY. This function never writes, moves,
# or deletes anything. Returns a hashtable of results for the caller
# (so the install phase can skip already-satisfied runtimes), and
# prints an aligned OK/MISSING/SKIP table with a conclusion block.
# ---------------------------------------------------------------------------
$Script:CheckRows = @()   # rows: [category, item, status, detail]

function Add-Row([string]$Category, [string]$Item, [string]$Status, [string]$Detail) {
    $Script:CheckRows += ,@($Category, $Item, $Status, $Detail)
}

# Probe one URL, return @{ Reachable = bool; Ms = int; Error = string }.
# HEAD first (cheap), GET as fallback (some mirrors 403 on HEAD).
function Test-UrlReachable([string]$Url) {
    $result = @{ Reachable = $false; Ms = -1; Error = "" }
    foreach ($method in @("HEAD", "GET")) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            # PS 5.1 has no -TimeoutSec-independent cancel; we rely on
            # TimeoutSec (default 10s, tunable via -ProbeTimeoutSec).
            if ($method -eq "HEAD") {
                Invoke-WebRequest -UseBasicParsing -Method Head -Uri $Url -TimeoutSec $ProbeTimeoutSec | Out-Null
            } else {
                # GET only headers is not possible in PS 5.1; fetch but
                # read just the status code. Small endpoints only.
                $resp = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $Url -TimeoutSec $ProbeTimeoutSec
                $resp.StatusCode | Out-Null
            }
            $sw.Stop()
            $result.Reachable = $true
            $result.Ms = [int]$sw.ElapsedMilliseconds
            return $result
        } catch {
            $sw.Stop()
            $result.Error = $_.Exception.Message
            # 405/403 style rejections on HEAD are "reachable" servers
            # -- a web server answering at all means the route works.
            $code = $null
            try { $code = [int]$_.Exception.Response.StatusCode } catch { }
            if ($code -and $code -ge 400 -and $code -lt 500) {
                $result.Reachable = $true
                $result.Ms = [int]$sw.ElapsedMilliseconds
                $result.Error = ""
                return $result
            }
        }
    }
    return $result
}

# Probe a python interpreter candidate: version string or $null.
# Wraps the v1 lesson: probe inside try/catch (never 2>$null redirect).
function Get-InterpreterVersion([string]$Exe) {
    $ver = $null
    try { $ver = (& $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])") } catch { }
    return $ver
}

# Resolve InstallDir for CHECK purposes WITHOUT creating it (Confirm-
# InstallDir creates; Phase 0 must not). Mirrors its defaulting logic.
function Resolve-InstallDirForCheck([string]$Dir) {
    if (-not $Dir -or -not $Dir.Trim()) {
        return (Join-Path $env:LOCALAPPDATA $Script:AppDirName)
    }
    $Dir = [Environment]::ExpandEnvironmentVariables($Dir)
    if (-not [IO.Path]::IsPathRooted($Dir)) {
        $Dir = Join-Path (Get-location) $Dir
    }
    return $Dir
}

# ---- v4: junction / symlink defense ----------------------------------
# FIELD INCIDENT (2026-10-10, real box): ui\node_modules\codeagent is a
# JUNCTION pointing back at the repo root (npm workspace self-link).
# robocopy follows junctions by default -> the installer copied the
# repo into itself recursively (6.6 GB junk from a 1.1 GB source).
# Linux cannot reproduce this layout, so every check below is written
# as a runtime ASSERT inside the script, never as an assumption.

# Find reparse points (junctions/symlinks) under a root. Metadata-only
# enumeration (no file contents read). IMPORTANT: walks with a manual
# stack and NEVER descends into a reparse point -- Get-ChildItem
# -Recurse would follow the junction and, on a self-link like the v4
# incident, recurse forever. Returns array of "path -> target" strings
# (empty array = clean).
function Find-ReparsePoints([string]$Root) {
    $found = @()
    if (-not (Test-Path -LiteralPath $Root)) { return $found }
    try {
        $stack = New-Object System.Collections.Stack
        $stack.Push((Get-Item -LiteralPath $Root -Force))
        while ($stack.Count -gt 0) {
            $dir = $stack.Pop()
            foreach ($child in (Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue)) {
                if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    $target = ""
                    try { $target = $child.Target } catch { }
                    $found += ("$($child.FullName) -> $target")
                    continue   # never traverse the link
                }
                if ($child.PSIsContainer) { $stack.Push($child) }
            }
        }
    } catch { }
    # comma: prevent PowerShell unrolling a single-element array into
    # a bare string (then $x[0] would be the first CHARACTER, not the
    # first entry -- caught by the v4 smoke test).
    return ,@($found)
}

# Bytes + file count of a tree, walking MANUALLY (not Get-ChildItem
# -Recurse, which follows junctions): the manual stack skips excluded
# dir names and every reparse point, mirroring robocopy /XJ /XD
# semantics. This is the ground truth the post-copy assert compares
# against. $ExcludeNames are leaf directory names to skip.
function Get-TreeStats([string]$Root, [string[]]$ExcludeNames) {
    $stats = @{ Bytes = [long]0; Files = 0; ReparseSkipped = 0 }
    if (-not (Test-Path -LiteralPath $Root)) { return $stats }
    $stack = New-Object System.Collections.Stack
    $stack.Push((Get-Item -LiteralPath $Root -Force))
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($child in (Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue)) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                # junction/symlink: NEVER traverse (mirrors /XJ)
                $stats.ReparseSkipped++
                continue
            }
            if ($child.PSIsContainer) {
                if ($ExcludeNames -contains $child.Name) { continue }
                $stack.Push($child)
            } else {
                $stats.Bytes += $child.Length
                $stats.Files++
            }
        }
    }
    return $stats
}

# v4: assert the copy did not blow up. $Expected/@{Bytes;Files} comes
# from Get-TreeStats on the SOURCE with the same exclusions; $Actual is
# the measured TARGET. If the target is more than 1.5x the source in
# bytes OR files, something followed a link or looped -- STOP, because
# the next thing it eats is the user's disk. (Incident: 6x / 6.6 GB.)
function Assert-CopySanity($Expected, $Actual, [string]$Where) {
    $ratioBytes = 0
    if ($Expected.Bytes -gt 0) { $ratioBytes = [double]$Actual.Bytes / [double]$Expected.Bytes }
    $ratioFiles = 0
    if ($Expected.Files -gt 0) { $ratioFiles = [double]$Actual.Files / [double]$Expected.Files }
    if ($ratioBytes -gt 1.5 -or $ratioFiles -gt 1.5) {
        throw @"
[install] COPY SANITY CHECK FAILED at $Where
  source:  $($Expected.Bytes) bytes, $($Expected.Files) files
  target:  $($Actual.Bytes) bytes, $($Actual.Files) files
  ratio:   bytes=$([math]::Round($ratioBytes,2)) files=$([math]::Round($ratioFiles,2))
  The target grew far beyond the source -- a junction/symlink loop was
  probably copied (see v4 incident: ui\node_modules\codeagent self-link,
  6.6 GB junk). The install has STOPPED. Inspect the target for
  ...\node_modules\<name>\... nesting loops, delete the target dir, and
  re-run. Do NOT let robocopy continue; it would eat the whole disk.
"@
    }
    Write-Host ("[install] copy sanity OK: target {0} bytes / {1} files vs source {2} / {3} (ratio {4}/{5})" -f `
        $Actual.Bytes, $Actual.Files, $Expected.Bytes, $Expected.Files, `
        [math]::Round($ratioBytes,2), [math]::Round($ratioFiles,2))
}

# v9 SECURITY: hard post-copy assertion -- the KEY defense line of the
# 2026-10-10 leak fix. The robocopy /XF exclusion list is belt; this is
# the suspenders. Runs after a FRESH app-tree copy. $EnvExistedBefore /
# $CfgExistedBefore say whether the target already owned these files
# BEFORE the copy (user-owned -> not our business, assertion skipped);
# when they were created/overwritten by the copy, they must be BLANK.
#   <InstallDir>\.env  : 0 bytes, or only comment/blank lines.
#   <InstallDir>\config.json : absent, OR byte-identical to
#       config.example.json, OR every model/api-key-ish field is empty
#       (parsed as JSON; on parse failure only the byte-identical
#       check counts).
# Any live credential content -> THROW "credentials leaked from source
# tree". Linux cannot reproduce the Windows robocopy layout, so this is
# a runtime assert, never an assumption.
function Assert-BlankConfig([string]$InstallDir, [bool]$EnvExistedBefore, [bool]$CfgExistedBefore, [string]$ExampleJson) {
    # --- .env ---
    $envDst = Join-Path $InstallDir ".env"
    if (-not $EnvExistedBefore) {
        if (-not (Test-Path -LiteralPath $envDst)) {
            # acceptable -- the template step below creates it blank;
            # but a source leak would have CREATED it, so an existing
            # file must pass the content check.
        } else {
            $raw = [IO.File]::ReadAllBytes($envDst)
            if ($raw.Length -gt 0) {
                $text = [Text.Encoding]::UTF8.GetString($raw)
                $dirty = $false
                foreach ($line in ($text -split "`r?`n")) {
                    $t = $line.Trim()
                    if ($t -ne "" -and -not $t.StartsWith("#")) { $dirty = $true; break }
                }
                if ($dirty) {
                    throw @"

[install] SECURITY FAILURE: credentials leaked from source tree.
  $envDst is non-empty and was written by the install copy
  ($($raw.Length) bytes). The source tree's .env must NEVER reach a
  fresh install. The install has STOPPED. Delete $InstallDir,
  verify the robocopy /XF .env exclusion in this script, and re-run.
"@
                }
            }
        }
    }
    # --- config.json ---
    $cfgDst = Join-Path $InstallDir "config.json"
    if (-not $CfgExistedBefore -and (Test-Path -LiteralPath $cfgDst)) {
        $cfgBytes = [IO.File]::ReadAllBytes($cfgDst)
        $ok = $false
        # check 1: byte-identical to the blank template
        if ((Test-Path -LiteralPath $ExampleJson)) {
            $exBytes = [IO.File]::ReadAllBytes($ExampleJson)
            if ($cfgBytes.Length -eq $exBytes.Length) {
                $same = $true
                for ($i = 0; $i -lt $cfgBytes.Length; $i++) {
                    if ($cfgBytes[$i] -ne $exBytes[$i]) { $same = $false; break }
                }
                if ($same) { $ok = $true }
            }
        }
        # check 2 (only if not identical): parse JSON and require every
        # key-ish field to be empty. Only fields that would make the
        # copy "live" count: api_key / token / secret / credential /
        # password / baseUrl / the model name. Non-secret user
        # settings (workspaceRoot, feature flags, apiKeyEnv -- that
        # one holds a variable NAME, not a value) may be non-empty:
        # they are config choices, not credentials.
        if (-not $ok) {
            try {
                $cfg = [Text.Encoding]::UTF8.GetString($cfgBytes) | ConvertFrom-Json
                $bad = @()
                $walk = {
                    param($node, $path)
                    if ($null -eq $node) { return }
                    if ($node -is [string]) { return }
                    if ($node -is [System.Collections.IEnumerable] -and $node -isnot [string]) {
                        $i = 0
                        foreach ($item in $node) { & $walk $item "$path[$i]"; $i++ }
                        return
                    }
                    # PSCustomObject: walk properties
                    foreach ($p in $node.PSObject.Properties) {
                        # key-ish / model-endpoint fields; apiKeyEnv is
                        # excluded on purpose (variable NAME, not value).
                        # v23 (t_d37fca26): use the EXACT credential
                        # name allowlist + secret-shaped VALUE check
                        # (same rules as Invoke-CredentialSweep) instead
                        # of the v21 fuzzy `.*_?token` suffix match, so
                        # non-secret config choices never false-throw.
                        $isLiveField = $p.Value -is [string] -and (
                            ($p.Name -match '(?i)^(api_?key(?!env)|access_?token|auth_?token|refresh_?token|session_?token|client_?secret|secret|secret_?key|password|passwd|passphrase|authorization|credential)$' -and (Test-SecretValue ([string]$p.Value))) -or
                            ($p.Name -match '(?i)^(base_?url|model|endpoint|url)$')
                        )
                        if ($p.Value -is [string] -and $isLiveField -and ([string]$p.Value).Trim() -ne "") {
                            $script:badLeaves += "$path.$($p.Name) = <non-empty>"
                        } elseif (-not ($p.Value -is [string])) {
                            & $walk $p.Value "$path.$($p.Name)"
                        }
                    }
                }
                $script:badLeaves = @()
                & $walk $cfg ""
                if ($script:badLeaves.Count -eq 0) { $ok = $true }
                else { $bad = $script:badLeaves }
            } catch { $bad = @("config.json is not valid JSON and not the blank template") }
            if (-not $ok) {
                $list = ($bad | Select-Object -First 5) -join "; "
                throw @"

[install] SECURITY FAILURE: credentials leaked from source tree.
  $cfgDst contains live values from the source tree ($($cfgBytes.Length)
  bytes; first findings: $list). The source tree's config.json must
  NEVER reach a fresh install. The install has STOPPED. Delete
  $InstallDir, verify the robocopy /XF config.json exclusion in this
  script, and re-run.
"@
            }
        }
    }
    # --- v21: credential sweep, ALL other files (not just .env /
    #     config.json) --- see Invoke-CredentialSweep for the rationale.
    Invoke-CredentialSweep -Root $InstallDir -SkipFiles @()
    Write-Host "[install] blank-config assertion OK: no credentials copied from the source tree"
}

# v21 (t_69654390): content-based credential sweep of a whole tree.
# The v9 assert only checked .env and config.json; the real field
# finding (config.json.bak-20261009-173158, 2894 B) sailed past both
# the /XF list and the assert. This sweep scans EVERY regular file:
#   (a) literal sk- key material: sk- followed by 8+ key chars, with a
#       non-alphanumeric BEFORE the "sk" (word boundary) so ordinary
#       words like "task-management" or "risk-assessment" do not match;
#   (b) any *.json with a NON-EMPTY api-key/token/secret/credential/
#       password field (apiKeyEnv holds a variable NAME, not a value,
#       and is exempt).
# Binary-looking files (NUL in first 8 KB, no UTF-16 BOM) and files
# >10 MB are skipped: keys do not live in electron/node_modules
# payloads, and false positives would hard-fail every install.
# $SkipFiles: absolute paths to leave alone (user-owned .env /
# config.json on the FINAL sweep -- a user's own filled-in config is
# not a leak). UTF-16LE (FF FE BOM) is decoded via the Unicode branch.
# v23 (t_d37fca26): is this JSON field VALUE shaped like a live
# secret? The v21 sweep flagged bare field NAMES (`unk_token` in a
# shipped tokenizer.json hard-stopped the installer). Now the name
# AND the value must both qualify. A value is a secret if it is an
# sk- key, a Bearer/Token authorization header, or a long opaque
# token (>=16 chars of key-ish charset). NOT secrets: "[UNK]",
# "[PAD]", short symbolic tokens, variable references ("${VAR}",
# "env:NAME"), URLs, null/true/false.
function Test-SecretValue([string]$Value) {
    $t = $Value.Trim().Trim('"').Trim("'")
    if ($t.Length -eq 0) { return $false }
    if ($t -match '(?i)^(?:sk|xk)-[A-Za-z0-9_\-]{8,}$') { return $true }
    if ($t -match '(?i)^(?:bearer|token|basic)\s+[A-Za-z0-9_\-.+/=]{8,}$') { return $true }
    if ($t -match '^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$') { return $false }       # variable NAME
    if ($t -match '(?i)^(?:env:|file:)[A-Za-z_][A-Za-z0-9_.-]*$') { return $false }
    if ($t -match '(?i)^https?://\S+$') { return $false }
    if ($t -match '(?i)^(?:none|null|true|false|undefined|n/?a)$') { return $false }
    if (-not ($t -cmatch '^[A-Z_][A-Z0-9_]*$')) { if ($t.Length -lt 16) { return $false } }  # "[UNK]", "[PAD]", short tokens; -cmatch = case-SENSITIVE (plain -match is CI in PS): mixed-case alnum values must stay live
    if ($t -cmatch '^[A-Z_][A-Z0-9_]*$') { return $false }                   # "DASHSCOPE_API_KEY" style env var NAME (exact case; -cmatch)
    return ($t -match '^[A-Za-z0-9_\-/+=:.@#$%^&*!~]{16,}$')                  # long opaque blob
}

# v23 (t_d37fca26): exact credential FIELD-NAME allowlist. The v21
# `.*_?token` / `.*_?secret` suffix match hit ANY word ending in
# "token" (unk_token, pad_token, cls_token in every HuggingFace
# tokenizer.json). The whole field name must now be one of these.
# (Kept as a literal inside Invoke-CredentialSweep so the function
# stays self-contained for the selfcheck harness.)
$script:LiveFieldNameRe = '(?i)^(api_?key(?!env)|access_?token|auth_?token|id_?token|refresh_?token|session_?token|client_?secret|secret|secret_?key|api_?secret|app_?secret|consumer_?secret|shared_?secret|password|user_?password|admin_?password|passwd|passphrase|private_?key|credential|credentials|authorization|auth)$'

# v23 (t_d37fca26): model/tokenizer artifacts are NEVER credential
# files, by name. tokenizer.json/vocab/merges/special_tokens_map
# etc. always carry unk_token/pad_token string fields; onnx is
# binary (also skipped by the NUL heuristic, this is belt+suspenders).
$script:ModelArtifactNameRe = '(?i)^(tokenizer|tokenizer_config|vocab|special_tokens_map|added_tokens|merges)\.(json|txt|bin)$|^(spiece|sentencepiece)\.(model|bin)$|\.onnx(\.data)?$'

function Invoke-CredentialSweep([string]$Root, [string[]]$SkipFiles) {
    $skPattern = '(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}'
    $liveFieldRe = if ($script:LiveFieldNameRe) { $script:LiveFieldNameRe } else { '(?i)^(api_?key(?!env)|access_?token|auth_?token|id_?token|refresh_?token|session_?token|client_?secret|secret|secret_?key|api_?secret|app_?secret|consumer_?secret|shared_?secret|password|user_?password|admin_?password|passwd|passphrase|private_?key|credential|credentials|authorization|auth)$' }
    $modelArtifactRe = if ($script:ModelArtifactNameRe) { $script:ModelArtifactNameRe } else { '(?i)^(tokenizer|tokenizer_config|vocab|special_tokens_map|added_tokens|merges)\.(json|txt|bin)$|^(spiece|sentencepiece)\.(model|bin)$|\.onnx(\.data)?$' }
    $script:credFindings = @()
    $sweepStack = New-Object System.Collections.Stack
    $sweepStack.Push((Get-Item -LiteralPath $Root -Force))
    while ($sweepStack.Count -gt 0) {
        $dir = $sweepStack.Pop()
        foreach ($child in (Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue)) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($child.PSIsContainer) { $sweepStack.Push($child); continue }
            if ($child.Length -eq 0) { continue }
            if ($child.Length -gt 10MB) { continue }  # payloads; keys never live here
            if ($SkipFiles -and ($SkipFiles -contains $child.FullName)) { continue }
            $raw = $null
            try { $raw = [IO.File]::ReadAllBytes($child.FullName) } catch { continue }
            # UTF16LE BOM check FIRST: a FF FE text file contains
            # interleaved NULs and the binary heuristic below would
            # wrongly skip it.
            $hasUtf16Bom = ($raw.Length -ge 2 -and $raw[0] -eq 0xFF -and $raw[1] -eq 0xFE)
            if (-not $hasUtf16Bom) {
                # binary heuristic: NUL in first 8 KB -> skip
                $probeLen = [Math]::Min(8192, $raw.Length)
                $isBinary = $false
                for ($i = 0; $i -lt $probeLen; $i++) { if ($raw[$i] -eq 0) { $isBinary = $true; break } }
                if ($isBinary) { continue }
                $text = [Text.Encoding]::UTF8.GetString($raw)
            } else {
                $text = [Text.Encoding]::Unicode.GetString($raw)
            }
            # (a) literal sk- key material
            $m = [regex]::Match($text, $skPattern)
            if ($m.Success) {
                $script:credFindings += ("{0}: literal key material `"{1}...`" ({2} bytes)" -f $child.FullName, $m.Value.Substring(0, [Math]::Min(6, $m.Value.Length)), $child.Length)
                continue
            }
            # (b) non-empty api-key-ish JSON field -- but NEVER on
            # dependency manifests: package.json / package-lock.json
            # contain non-secret "tokens"/"token" fields (e.g.
            # typescript's "@esfx/canceltoken" deps) that would
            # hard-fail every install. Config-ish files only.
            # v23 (t_d37fca26): model/tokenizer artifacts (incl.
            # tokenizer.json) are skipped by NAME -- their
            # unk_token/pad_token fields are symbolic tokens, never
            # secrets, and the field-name regex alone would still
            # string-match nested ids.
            $isManifest = $child.Name -match '(?i)^(package|package-lock|npm-shrinkwrap)\.json$'
            $isModelArtifact = $child.Name -match $modelArtifactRe
            if ($child.Extension -eq ".json" -and -not $isManifest -and -not $isModelArtifact) {
                try {
                    $obj = $text | ConvertFrom-Json -ErrorAction Stop
                    $script:badLeaves = @()
                    $walkCfg = {
                        param($node)
                        if ($null -eq $node) { return }
                        if ($node -is [string]) { return }
                        if ($node -is [System.Collections.IEnumerable] -and $node -isnot [string]) {
                            foreach ($item in $node) { & $walkCfg $item }
                            return
                        }
                        foreach ($p in $node.PSObject.Properties) {
                            # v23: BOTH the field NAME (exact credential
                            # allowlist, above) AND the VALUE (secret
                            # SHAPE, Test-SecretValue) must qualify --
                            # unk_token="..." fails the name check, and
                            # short/ symbolic values fail the shape check.
                            if ($p.Value -is [string] -and $p.Name -match $liveFieldRe -and (Test-SecretValue ([string]$p.Value))) {
                                $script:badLeaves += $p.Name
                            } elseif (-not ($p.Value -is [string])) {
                                & $walkCfg $p.Value
                            }
                        }
                    }
                    & $walkCfg $obj
                    if ($script:badLeaves.Count -gt 0) {
                        $script:credFindings += ("{0}: live field(s) {1} ({2} bytes)" -f $child.FullName, (($script:badLeaves | Select-Object -First 3) -join ","), $child.Length)
                    }
                } catch { }  # not JSON -> name-based /XF already governed it
            }
        }
    }
    if ($script:credFindings.Count -gt 0) {
        $list = ($script:credFindings | Select-Object -First 8) -join [Environment]::NewLine + "  "
        throw @"

[install] SECURITY FAILURE: credentials leaked from source tree.
  The install tree contains live credential material:
  $list
  The source tree must NEVER ship .env / config.json / *.bak / backup
  copies of them (see the v21 copy /XF list and this content sweep).
  The install has STOPPED. Delete $InstallDir, clean the source tree
  of dev backups, and re-run.
"@
    }
    Write-Host "[install] credential sweep OK: no sk- key material and no valued apiKey field anywhere under $Root"
}


function Invoke-EnvironmentCheck([string]$CheckInstallDir) {
    $Script:CheckRows = @()
    # v7: resolve the package dir for the check (WITHOUT creating
    # it and WITHOUT write probes -- Phase 0 is strictly read-only).
    $checkPackageDir = $(if ($PackageDir) { Resolve-PathSafe $PackageDir } else { Join-Path $CheckInstallDir "packages" })
    # --- outcome flags the install phase will consume ---
    $out = @{
        HasUsablePython = $false
        HasRuntimePython = $false
        HasRuntimeNode   = $false
        HasVenv          = $false
        HasNodeModules   = $false
        HasMarker        = $false
        DiskFreeMB       = 0
        RunningProcesses = @()
        MirrorFailures   = @()
    }

    # 1. system -----------------------------------------------------------
    $os = [Environment]::OSVersion.Version
    Add-Row "system" "Windows version" "OK" ("$os (PS $($PSVersionTable.PSVersion))")
    $arch = "unknown"
    try { $arch = $env:PROCESSOR_ARCHITECTURE } catch { }
    Add-Row "system" "OS architecture" "OK" $arch
    $isAdmin = $false
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { }
    Add-Row "system" "Running as admin" "OK" ($(if ($isAdmin) { "yes (not required)" } else { "no (fine -- installer is zero-permission)" }))

    # v7: the three resolved paths + existence + free space each.
    # (Drive free space for each; the "target" drive row below stays
    # for the disk-need estimate.)
    $out.PackageDir = $checkPackageDir
    $dataDirCheck = $(if ($DataDir) { [Environment]::ExpandEnvironmentVariables($DataDir) } else { Join-Path $env:APPDATA $Script:AppDirName })
    foreach ($p in @(
        @("InstallDir", $CheckInstallDir),
        @("PackageDir", $checkPackageDir),
        @("DataDir",    $dataDirCheck)
    )) {
        $name = $p[0]; $path = $p[1]
        $exists = Test-Path -LiteralPath $path
        $free = ""
        if ($exists) {
            try {
                $drive = [IO.Path]::GetPathRoot((Get-Item -LiteralPath $path -Force).FullName)
                $free = ", $([int]((Get-PSDrive -Name ($drive.TrimEnd(':\')) -ErrorAction Stop).Free / 1MB)) MB free on its drive"
            } catch { }
        }
        Add-Row "paths" $name "OK" ("$path (exists: $(if ($exists) { 'yes' } else { 'no' })$free)")
    }

    # disk: InstallDir's drive + %TEMP% drive
    # v8: resolve the drive letter from the path TEXT even when the
    # directory does not exist yet (fresh install): take the root of
    # the (possibly non-existent) path and query Get-PSDrive by the
    # drive letter. UNC paths (\\server\share) still SKIP -- no drive
    # letter to query.
    $Script:LastDriveFreeError = ""
    foreach ($pair in @(@("target", $CheckInstallDir), @("temp", ([IO.Path]::GetTempPath())))) {
        $label = $pair[0]; $dir = $pair[1]
        try {
            $drive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($dir))
            if ($drive -notmatch '^[A-Za-z]:\\$') {
                Add-Row "system" "disk space ($label dir)" "SKIP" "not a drive-letter path (UNC/root): $dir"
                continue
            }
            $letter = $drive.Substring(0, 1)
            $free = [int]((Get-PSDrive -Name $letter -ErrorAction Stop).Free / 1MB)
            if ($label -eq "target") { $out.DiskFreeMB = $free }
            $need = 700   # MB, rough: python runtime ~70 + node ~35 + venv deps ~200 + app ~100, with headroom
            $status = "OK"; $detail = "$free MB free on $drive (need ~$need MB)"
            if ($free -lt $need) { $status = "MISSING"; $detail = "$free MB free on $drive -- BELOW ~$need MB estimate; free disk space or pick another drive" }
            Add-Row "system" "disk space ($label dir)" $status $detail
        } catch {
            Add-Row "system" "disk space ($label dir)" "SKIP" ("could not resolve free space for " + $dir + " (" + $_.Exception.Message + ")")
        }
    }

    # 2. interpreters -----------------------------------------------------
    # v8: full discovery (PATH + registry + common locations + py
    # launcher). EVERY found interpreter gets a row; the selected one
    # is marked. This runs Find-Python, which EXECUTES each candidate
    # exe once (read-only probe).
    $pyDecision = Find-Python $PythonPath $CheckInstallDir
    foreach ($c in $pyDecision.Candidates) {
        if ($c.Selected) {
            Add-Row "python" "interpreter: $($c.Exe)" "OK" "WILL USE -- $($c.Source), Python $($c.Version)"
        } elseif ($c.SkipReason) {
            # NOTE: PowerShell 5.1 evaluates a $(if ...) subexpression that
            # contains backslash-escaped quotes as if it were a command
            # (parse says 0 errors, runtime throws CommandNotFound). Build
            # the fragment first -- same lesson as the launcher bug.
            $verFrag = ""
            if ($c.Version) { $verFrag = "Python $($c.Version), " }
            Add-Row "python" "interpreter: $($c.Exe)" "SKIP" "$($c.Source), $verFrag$($c.SkipReason)"
        } else {
            Add-Row "python" "interpreter: $($c.Exe)" "OK" "$($c.Source), Python $($c.Version) (usable, not selected -- lower precedence)"
        }
    }
    if (-not $pyDecision.Candidates -or $pyDecision.Candidates.Count -eq 0) {
        Add-Row "python" "interpreters found" "MISSING" "none discovered (PATH/registry/common locations all empty) -- installer will bootstrap the pinned official $PythonVersion"
    }
    # v11: when conda BASE was seen but skipped, say how to opt in.
    $condaSkipped = $false
    foreach ($c in $pyDecision.Candidates) {
        if ($c.SkipReason -like 'conda BASE*') { $condaSkipped = $true }
    }
    if ($condaSkipped -and -not $AllowCondaBase) {
        Add-Row "python" "conda base policy" "SKIP" "conda BASE is skipped by default (shared install); to build the venv on it add -AllowCondaBase, or point -PythonPath at any env's python"
    }
    Add-Row "python" "decision" "OK" $pyDecision.Reason
    $foundPy = $pyDecision.Exe
    $out.HasUsablePython = [bool]$foundPy
    $out.PyDecision = $pyDecision
    $checkResult_PyExe = $foundPy   # v8: for the conclusion block

    # v10: same-version official-installer registration state (the
    # WixBundleInstalled silent-skip guard). Read-only probes; an
    # already-registered interpreter is EXECUTED once to check it
    # is alive (same discipline as the candidate probes above).
    # v20: -IgnoreRegistryState skips the gate entirely.
    if ($IgnoreRegistryState) {
        $out.RegAction = "ignored"
        $out.RegExe = $null
        $out.RegSources = @()
        $regDetail = "-IgnoreRegistryState: the pinned-version registration gate is SKIPPED by explicit request (broken/dangling registrations whose repair+uninstall both fail)"
        $regStatus = "SKIP"
        $regCheck = @{ Action = "ignored"; Exe = $null; Sources = @() }
        Add-Row "python" "official $PythonVersion registration" $regStatus $regDetail
    } else {
    $regCheck = Test-PinnedPythonRegistration $PythonVersion ([bool]$ForceOwnRuntime)
    $out.RegAction = $regCheck.Action
    $out.RegExe = $regCheck.Exe
    $out.RegSources = $regCheck.Sources
    $regDetail = "no registry trace of Python $PythonVersion (safe to run the official installer)"
    switch ($regCheck.Action) {
        "reuse" {
            $regDetail = "Python $PythonVersion is already REGISTERED and its interpreter runs -- will REUSE it for the venv, no installer download"
            if (-not $foundPy) {
                $out.HasUsablePython = $true
                $checkResult_PyExe = $regCheck.Exe
            }
        }
        "broken" {
            $regDetail = "Python $PythonVersion is REGISTERED but broken/missing (dangling) -- the official installer would SILENTLY SKIP (WixBundleInstalled). FIX FIRST before installing"
        }
        "conflict" {
            $regDetail = "-ForceOwnRuntime + Python $PythonVersion already registered: a second same-version copy is IMPOSSIBLE (WiX per-version single instance). FIX FIRST"
        }
        default { }
    }
    $regStatus = "OK"
    if ($regCheck.Action -eq "broken" -or $regCheck.Action -eq "conflict") { $regStatus = "MISSING" }
    Add-Row "python" "official $PythonVersion registration" $regStatus $regDetail
    }   # v20: end of the -IgnoreRegistryState else
    foreach ($name in @("node", "npm")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = $null
            try { $ver = (& $cmd.Source --version) } catch { }
            if ($ver) { Add-Row "node" $name "OK" "$ver at $($cmd.Source)" }
            else { Add-Row "node" $name "MISSING" "present but failed to run" }
        } else {
            Add-Row "node" $name "MISSING" "not on PATH (optional; installer can bootstrap a private runtime)"
        }
    }
    $tarCmd = Get-Command tar -ErrorAction SilentlyContinue
    if ($tarCmd) { Add-Row "tools" "tar" "OK" $tarCmd.Source }
    else { Add-Row "tools" "tar" "MISSING" "not on PATH (needed to extract the python runtime; Win10 1803+ ships bsdtar)" }

    # 3. existing install -------------------------------------------------
    if (Test-Path -LiteralPath $CheckInstallDir) {
        Add-Row "install" "<InstallDir> exists" "OK" $CheckInstallDir
        $runtimePy = Join-Path $CheckInstallDir "runtime\python\python.exe"
        $out.HasRuntimePython = Test-Path -LiteralPath $runtimePy
        if ($out.HasRuntimePython) {
            $ver = Get-InterpreterVersion $runtimePy
            Add-Row "install" "runtime\python" "OK" "present (Python $ver) -- install will reuse, no re-download"
        } else {
            Add-Row "install" "runtime\python" "MISSING" "not present"
        }
        $runtimeNode = Join-Path $CheckInstallDir "runtime\node\node.exe"
        $out.HasRuntimeNode = Test-Path -LiteralPath $runtimeNode
        if ($out.HasRuntimeNode) {
            $ver = $null; try { $ver = (& $runtimeNode --version) } catch { }
            Add-Row "install" "runtime\node" "OK" "present ($ver) -- install will reuse, no re-download"
        } else {
            Add-Row "install" "runtime\node" "MISSING" "not present"
        }
        $venvPy = Join-Path $CheckInstallDir "python\.venv\Scripts\python.exe"
        $out.HasVenv = Test-Path -LiteralPath $venvPy
        if ($out.HasVenv) { Add-Row "install" "python\.venv" "OK" "present -- install will reuse, no rebuild" }
        else { Add-Row "install" "python\.venv" "MISSING" "not present" }
        $bat = Join-Path $CheckInstallDir "CodeAgent.bat"
        if (Test-Path -LiteralPath $bat) { Add-Row "install" "CodeAgent.bat" "OK" "present (re-run will overwrite/update)" }
        else { Add-Row "install" "CodeAgent.bat" "MISSING" "not present" }
        $out.HasMarker = Test-Path -LiteralPath (Join-Path $CheckInstallDir ".install-state.json")
        if ($out.HasMarker) { Add-Row "install" ".install-state.json" "OK" "present -- app files already copied once" }
        else { Add-Row "install" ".install-state.json" "MISSING" "not present (fresh copy will run)" }
        $nm = Join-Path $CheckInstallDir "node_modules\electron\dist\electron.exe"
        $out.HasNodeModules = Test-Path -LiteralPath $nm
        if ($out.HasNodeModules) { Add-Row "install" "node_modules\electron" "OK" "bundled electron.exe present -- app runs without system Node" }
        else { Add-Row "install" "node_modules\electron" "MISSING" "not present (will be copied from source, or npm install needed)" }
    } else {
        Add-Row "install" "<InstallDir> exists" "MISSING" "no ($CheckInstallDir) -- fresh install"
        Add-Row "install" "runtime\python" "MISSING" "not present"
        Add-Row "install" "runtime\node" "MISSING" "not present"
        Add-Row "install" "python\.venv" "MISSING" "not present"
    }

    # data dir
    $dataDir = $(if ($DataDir) { [Environment]::ExpandEnvironmentVariables($DataDir) } else { Join-Path $env:APPDATA $Script:AppDirName })
    $out.DataDir = $dataDir
    if (Test-Path -LiteralPath $dataDir) { Add-Row "install" "data dir" "OK" "$dataDir (exists -- will be REUSED, pass -PurgeData on -Uninstall to wipe)" }
    else { Add-Row "install" "data dir" "OK" "$dataDir (will be created by Electron on first launch)" }

    # 4. running processes (read-only probe; empty on non-Windows) --------
    $running = @()
    try {
        # Get-Process is safe/read-only; match by name substring.
        foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) {
            if ($p.ProcessName -match '^(CodeAgent|electron)$') { $running += "$($p.ProcessName)#$($p.Id)" }
        }
    } catch { }
    $out.RunningProcesses = $running
    if ($running.Count -gt 0) {
        Add-Row "install" "running processes" "MISSING" ("CodeAgent/electron running: " + ($running -join ', ') + " -- close it before installing/updating")
    } else {
        Add-Row "install" "running processes" "OK" "none detected"
    }

    # 4b. v4: junction / symlink scan (source tree + target dir) ----------
    # FIELD INCIDENT: ui\node_modules\codeagent junction -> repo root,
    # robocopy followed it, 6.6 GB junk. We report every reparse point
    # found; robocopy now carries /XJ so they are skipped, and the
    # post-copy sanity assert catches anything that slips through.
    $sourceRoot = Split-Path -Parent $PSScriptRoot
    $out.SourceReparse = @()
    if (Test-Path -LiteralPath $sourceRoot) {
        $out.SourceReparse = Find-ReparsePoints $sourceRoot
        if ($out.SourceReparse.Count -gt 0) {
            Add-Row "source" "junctions/symlinks" "OK" ("$($out.SourceReparse.Count) found -- copy will SKIP them (/XJ); first: " + $out.SourceReparse[0])
        } else {
            Add-Row "source" "junctions/symlinks" "OK" "none found (copy runs without /XJ effect)"
        }
    } else {
        Add-Row "source" "junctions/symlinks" "SKIP" "source root not found: $sourceRoot"
    }

    # 4a2. v9 SECURITY: source credentials detection. The copy EXCLUDES
    # .env / config.json by name (/XF); the target always starts from
    # a blank config the user fills in themselves. This row exists so
    # a dev running from a dirty source tree SEES the leak risk before
    # anything is copied.
    $srcEnv = Join-Path $sourceRoot ".env"
    $srcCfg = Join-Path $sourceRoot "config.json"
    $srcEnvSize = 0; $srcCfgSize = 0
    if (Test-Path -LiteralPath $srcEnv) { $srcEnvSize = (Get-Item -LiteralPath $srcEnv -Force).Length }
    if (Test-Path -LiteralPath $srcCfg) { $srcCfgSize = (Get-Item -LiteralPath $srcCfg -Force).Length }
    if ($srcEnvSize -gt 0 -or $srcCfgSize -gt 0) {
        $note = "detected in SOURCE tree -- .env ($srcEnvSize bytes) / config.json ($srcCfgSize bytes) are EXCLUDED from the copy (/XF);"
        $note += " target starts with a BLANK config: fill in <InstallDir>\config.json (models) and <InstallDir>\.env (keys) after install"
        Add-Row "source" "credentials (.env/config.json)" "OK" $note
        $out.SourceHasCredentials = $true
    } else {
        Add-Row "source" "credentials (.env/config.json)" "OK" "none found in source tree (copy is clean by construction)"
        $out.SourceHasCredentials = $false
    }
    if ($out.HasMarker) {
        # v9: marker present means the app-tree copy already ran; a
        # re-run does not re-copy, so the user's own .env/config.json
        # at the target survive (and are never overwritten).
        Add-Row "install" "user config preserved" "OK" "app files already copied -- target .env / config.json are user-owned, never overwritten"
    }
    if (Test-Path -LiteralPath $CheckInstallDir) {
        $tgtReparse = Find-ReparsePoints $CheckInstallDir
        if ($tgtReparse.Count -gt 0) {
            Add-Row "install" "target junctions/symlinks" "MISSING" ("$($tgtReparse.Count) found in target -- could loop on re-copy; first: " + $tgtReparse[0])
            $out.UserMustFixPre = @("remove stale junctions/symlinks inside $CheckInstallDir before re-installing (see target rows above)")
        } else {
            Add-Row "install" "target junctions/symlinks" "OK" "none found"
        }
    }

    # 4c. v4: disk-usage estimate = measured source size + runtime
    # downloads, against the target drive's free space.
    $copyExcludes = @("node_modules", ".venv", ".git", "runtime", ".install-state.json")
    $srcStats = Get-TreeStats $sourceRoot $copyExcludes
    $srcMB = [int][math]::Round($srcStats.Bytes / 1MB)
    $nmStats = $null
    $nmSrc = Join-Path $sourceRoot "node_modules"
    if (Test-Path -LiteralPath $nmSrc) { $nmStats = Get-TreeStats $nmSrc @() }
    $nmMB = 0
    if ($nmStats) { $nmMB = [int][math]::Round($nmStats.Bytes / 1MB) }
    $out.SourceCopyMB = $srcMB
    $out.NodeModulesMB = $nmMB
    Add-Row "source" "app tree size (post-exclusion)" "OK" ("$srcMB MB, $($srcStats.Files) files, $($srcStats.ReparseSkipped) link(s) skipped")
    if ($nmStats) {
        Add-Row "source" "node_modules size (electron runtime)" "OK" "$nmMB MB -- copied as-is"
    }
    # total footprint estimate: app copy + node_modules + venv deps +
    # runtimes. Compare against DiskFreeMB from the probe above.
    $footprintMB = $srcMB + $nmMB
    if (-not $out.HasRuntimePython) { $footprintMB += 80 }
    if (-not $out.HasVenv) { $footprintMB += 150 }
    if (-not $NoNode -and -not $out.HasRuntimeNode) { $footprintMB += 35 }
    $out.FootprintMB = $footprintMB
    if ($out.DiskFreeMB -gt 0) {
        if ($footprintMB -gt ($out.DiskFreeMB * 2)) {
            Add-Row "source" "disk footprint estimate" "MISSING" ("~$footprintMB MB needed vs $($out.DiskFreeMB) MB free -- pick another drive or free space")
        } else {
            Add-Row "source" "disk footprint estimate" "OK" "~$footprintMB MB total (app + node_modules + runtimes + venv) vs $($out.DiskFreeMB) MB free"
        }
    }

    # 4d. v7: package inventory -- which bootstrap packages are
    # present/missing in the package dir, with size and a read-only
    # verification verdict (size band + magic; full sha/signature
    # checks run at install time, not here).
    $pkgChecks = @(
        @{ Kind = "PythonInstaller"; Size = $Script:PyInstallerBytes; Magic = "4D5A";     Use = ($(if ($UsePbs) { "only with -UsePbs fallback" } else { "python bootstrap (official installer)" })) }
        @{ Kind = "PbsArchive";       Size = [long]0;                 Magic = "1F8B";     Use = "python bootstrap (-UsePbs only)" }
        @{ Kind = "NodeZip";          Size = $Script:NodeZipBytes;    Magic = "504B0304"; Use = "node bootstrap (optional)" }
        @{ Kind = "NodeSums";         Size = [long]0;                 Magic = "";         Use = "node zip sha256 source" }
    )
    $out.PackagesPresent = @()
    $out.PackagesMissing = @()
    if (Test-Path -LiteralPath $checkPackageDir) {
        foreach ($p in $pkgChecks) {
            $fname = Get-PackageFileName $p.Kind
            $pfile = Join-Path $checkPackageDir $fname
            if (-not (Test-Path -LiteralPath $pfile)) {
                Add-Row "packages" $fname "MISSING" "not in $checkPackageDir (will download: $($p.Use))"
                $out.PackagesMissing += $fname
                continue
            }
            $len = (Get-Item -LiteralPath $pfile -Force).Length
            $verdict = "OK"
            $note = ""
            if ($p.Magic) {
                $ok = $false
                try {
                    $fs = [IO.File]::OpenRead($pfile)
                    try {
                        $need = $p.Magic.Length / 2
                        $buf = New-Object byte[] $need
                        $got = $fs.Read($buf, 0, $need)
                        if ($got -ge $need) {
                            $actual = ([BitConverter]::ToString($buf)).Replace('-', '')
                            if ($actual -eq $p.Magic) { $ok = $true }
                        }
                    } finally { $fs.Dispose() }
                } catch { }
                if (-not $ok) { $verdict = "MISSING"; $note = " -- WRONG MAGIC BYTES, will NOT be used (re-download)" }
            }
            if ($verdict -eq "OK" -and $p.Size -gt 0) {
                $lo = [long]($p.Size * 0.5); $hi = [long]($p.Size * 1.5)
                if ($len -lt $lo -or $len -gt $hi) {
                    $verdict = "MISSING"
                    $note = " -- SIZE OUT OF BAND ($len vs ~$($p.Size)), will NOT be used (re-download)"
                }
            }
            $sizeMB = [math]::Round($len / 1MB, 1)
            Add-Row "packages" $fname $verdict ("present, $sizeMB MB ($($p.Use))$note -- $(if ($verdict -eq 'OK') { 'reusable, passes local checks' } else { 'NOT usable, will re-download' })")
            if ($verdict -eq "OK") { $out.PackagesPresent += $fname } else { $out.PackagesMissing += $fname }
        }
        # wheels subdirectory (v7 default -WheelDir location)
        $wheelsDir = Join-Path $checkPackageDir "wheels"
        if (Test-Path -LiteralPath $wheelsDir) {
            $wCount = @(Get-ChildItem -LiteralPath $wheelsDir -Filter *.whl -ErrorAction SilentlyContinue).Count
            Add-Row "packages" "wheels\" "OK" "$wCount wheel file(s) -- offline pip will use them"
        } else {
            Add-Row "packages" "wheels\" "MISSING" "no wheels dir under the package dir (online pip unless -WheelDir)"
        }
    } else {
        Add-Row "packages" "package dir" "MISSING" "no $checkPackageDir -- packages will be downloaded (nothing cached)"
        $out.PackagesMissing += "package dir"
    }

    # 5. mirrors ----------------------------------------------------------
    $pyUrl = $PythonInstallerMirror.TrimEnd('/')
    $nodeUrl = $NodeMirror.TrimEnd('/')
    $pipTargets = @(
        @{ Cat = "mirror"; Item = "python mirror (official $PythonVersion installer)"; Url = $pyUrl }
        @{ Cat = "mirror"; Item = "node mirror ($NodeVersion, pinned)";            Url = $nodeUrl }
        @{ Cat = "mirror"; Item = "pip mirror ($IndexUrl)";                        Url = $IndexUrl }
    )
    $pyPkgOk = ($out.PackagesPresent -contains (Get-PackageFileName "PythonInstaller")) -or ($out.PackagesPresent -contains (Get-PackageFileName "PbsArchive"))
    $nodePkgOk = ($out.PackagesPresent -contains (Get-PackageFileName "NodeZip"))
    foreach ($t in $pipTargets) {
        if ($Offline -or $PythonArchive -or $NodeArchive) {
            Add-Row $t.Cat $t.Item "SKIP" "offline/archive mode -- no network download for this"
            continue
        }
        # v7: a verified cached package makes its mirror moot for
        # THIS run (reuse path); pip's index still matters for deps.
        if ($t.Item -like "python mirror*" -and $pyPkgOk) {
            Add-Row $t.Cat $t.Item "SKIP" "python package already cached in the package dir -- no download needed"
            continue
        }
        if ($t.Item -like "node mirror*" -and $nodePkgOk) {
            Add-Row $t.Cat $t.Item "SKIP" "node zip already cached in the package dir -- no download needed"
            continue
        }
        $r = Test-UrlReachable $t.Url
        if ($r.Reachable) { Add-Row $t.Cat $t.Item "OK" ("reachable, $($r.Ms) ms") }
        else {
            Add-Row $t.Cat $t.Item "MISSING" ("UNREACHABLE ($($r.Error)) -- alternatives: -PythonArchive/-NodeArchive local files, another mirror via -PbsMirror/-NodeMirror/-IndexUrl")
            $out.MirrorFailures += $t.Item
        }
    }

    # 6. print the table --------------------------------------------------
    Write-Host ""
    Write-Host "=========== Phase 0: environment check ==========="
    $catPad = 8; $itemPad = 24; $statusPad = 8
    Write-Host ("{0,-$catPad} {1,-$itemPad} {2,-$statusPad} {3}" -f "GROUP", "ITEM", "STATUS", "DETAIL")
    Write-Host ("-" * 100)
    foreach ($row in $Script:CheckRows) {
        Write-Host ("{0,-$catPad} {1,-$itemPad} {2,-$statusPad} {3}" -f $row[0], $row[1], $row[2], $row[3])
    }
    Write-Host ("-" * 100)

    # 7. conclusion -------------------------------------------------------
    # v8: the python conclusion follows the DECISION (which now sees
    # PATH, registry, and common locations), not just HasRuntimePython.
    $mustInstall = @()
    $willSkip = @()
    if ($out.HasRuntimePython) {
        $willSkip += "python runtime (already installed at <InstallDir>\runtime\python)"
    } elseif ($out.HasUsablePython) {
        $willSkip += "python runtime: NO DOWNLOAD -- using the interpreter found at $($checkResult_PyExe) (see decision row above)"
    } elseif ($ForceOwnRuntime) {
        $mustInstall += "python runtime: -ForceOwnRuntime -- OFFICIAL Python $PythonVersion installer (~27 MB download), mirrors: Tsinghua -> Aliyun, Authenticode-verified (ignoring any pre-existing interpreter)"
    } else {
        $mustInstall += "python runtime: OFFICIAL Python $PythonVersion installer (~27 MB download), mirrors: Tsinghua -> Aliyun, Authenticode-verified"
    }
    if (-not $out.HasVenv) { $mustInstall += "venv + pip deps (~150 MB wheels, mirror download; venv pip.ini pins the mirror)" }
    else { $willSkip += "venv (already built)" }
    if (-not $NoNode -and -not $out.HasRuntimeNode) { $mustInstall += "node runtime: PINNED $NodeVersion (~36 MB download, npmmirror, sha256-verified, OPTIONAL)" }
    elseif ($out.HasRuntimeNode) { $willSkip += "node runtime (already installed)" }
    if (-not $out.HasMarker) { $mustInstall += "app files + node_modules (~350 MB from source copy)" }
    else { $willSkip += "app files (marker present)" }

    $estMB = 0
    if (-not $out.HasRuntimePython) { $estMB += 27 }
    if (-not $out.HasVenv) { $estMB += 150 }
    if (-not $NoNode -and -not $out.HasRuntimeNode -and -not $NodeArchive) { $estMB += 36 }
    if (-not $out.HasMarker) { $estMB += 0 }   # app copy is local, not network

    if ($Offline) {
        # v16: the conclusion must MATCH actual offline behavior -- offline
        # installs never download; whatever is not available locally shows
        # up as a FIX FIRST pointer, not as a "TO INSTALL ... download".
        $onlineMustInstall = @($mustInstall)
        $onlineWillSkip = @($willSkip)
        $mustInstall = @()
        $willSkip = $onlineWillSkip
        foreach ($m in $onlineMustInstall) {
            if ($m -like "python runtime*") {
                if ($PythonArchive) { $mustInstall += "python runtime: from -PythonArchive ($PythonArchive) -- NO network download" }
                elseif ($out.HasRuntimePython) { $mustInstall += "python runtime: already laid at <InstallDir>\runtime\python -- reuse" }
                else { $mustInstall += "python runtime: from the package dir cache ($(Get-PackageFileName "PythonInstaller")) if present + verified -- NO network download; otherwise a FIX FIRST blocker" }
            } elseif ($m -like "venv*") {
                if ($WheelDir) { $mustInstall += "venv + pip deps: OFFLINE from $WheelDir (pip --no-index --find-links) -- NO network download" }
                elseif ($out.HasVenv) { $mustInstall += "venv + pip deps: existing venv reused (no pip run)" }
                else { $mustInstall += "venv + pip deps: NO wheels available -- offline pip cannot run; a FIX FIRST blocker (copy wheels into <PackageDir>\wheels or pass -WheelDir)" }
            } elseif ($m -like "node runtime*") {
                if ($NodeArchive) { $mustInstall += "node runtime: from -NodeArchive ($NodeArchive) -- NO network download" }
                elseif ($out.HasRuntimeNode) { $mustInstall += "node runtime: already laid -- reuse" }
                else { $mustInstall += "node runtime: from the package dir cache if present + verified -- NO network download; otherwise node is SKIPPED (optional)" }
            } else {
                # app files / anything local
                $mustInstall += (($m -replace "\(.*$", "").Trim() + " (local, no download)")
            }
        }
        $estMB = 0   # offline = no network bytes
    }

    Write-Host ""
    Write-Host "Conclusion:"
    foreach ($m in $mustInstall) { Write-Host "  TO INSTALL:  $m" }
    foreach ($s in $willSkip)   { Write-Host "  SKIP:        $s" }
    Write-Host ("  ESTIMATED DOWNLOAD: ~{0} MB (network) + local copy time" -f $estMB)
    Write-Host ("  ESTIMATED DISK FOOTPRINT: ~{0} MB on the target drive (app {1} + node_modules {2} + runtimes/venv)" -f $out.FootprintMB, $out.SourceCopyMB, $out.NodeModulesMB)
    $userMustFix = @()
    if ($out.UserMustFixPre) { $userMustFix += $out.UserMustFixPre }
    # v10: broken / conflicting same-version registration is a FIX
    # FIRST item, not a silent skip.
    if ($out.RegAction -eq "broken") {
        # v20: the old /repair+/uninstall recipe FAILED on the the field box
        # field box (1602/1603); re-point the guidance at the routes
        # that actually work.
        $userMustFix += ("BROKEN Python $PythonVersion registration (registry says installed, files missing) -- the official-installer route would SILENTLY SKIP (WixBundleInstalled). EASIEST: re-run with -UsePbs (portable, no registration) or -IgnoreRegistryState; to clean the registration for real use msiexec /x {GUID} per ARP/Dependencies entry, then delete HKCU\Software\Python\PythonCore\3.12")
    }
    if ($out.RegAction -eq "conflict") {
        $userMustFix += ("-ForceOwnRuntime with Python $PythonVersion already registered: the official installer cannot lay a second same-version copy -- use -UsePbs (portable, no registration) or a different patch version (e.g. 3.12.9)")
    }
    if ($out.RegAction -eq "reuse") {
        $willSkip += "python runtime: Python $PythonVersion already registered (interpreter at $($out.RegExe)) -- REUSING it, no installer download"
    }
    if ($out.DiskFreeMB -gt 0 -and $out.DiskFreeMB -lt 700) { $userMustFix += "free disk space on the target drive (see disk rows above)" }
    if ($out.RunningProcesses.Count -gt 0) { $userMustFix += "close the running CodeAgent/electron processes" }
    if ($out.MirrorFailures.Count -gt 0) {
        $userMustFix += "unreachable mirrors: use -PbsMirror/-NodeMirror/-IndexUrl to switch, or -PythonArchive/-NodeArchive/-WheelDir for offline"
    }
    if (-not $out.HasRuntimePython -and -not $out.HasUsablePython -and $out.MirrorFailures.Count -gt 0 -and -not $ForceOwnRuntime) {
        $userMustFix += "no Python on PATH AND python mirror unreachable -- download python-$PythonVersion-amd64.exe manually and pass -PythonArchive (with -UsePbs, a cpython PBS install_only.tar.gz)"
    }
    if ($userMustFix.Count -eq 0) {
        Write-Host "  BLOCKERS: none -- ready to install."
    } else {
        Write-Host "  FIX FIRST (user action required):"
        foreach ($f in $userMustFix) { Write-Host "    * $f" }
    }
    Write-Host "=================================================="
    $out.UserMustFix = $userMustFix
    return $out
}

# ---------------------------------------------------------------------------
# runtime bootstrap (v2 core -- NOT field-tested on a real Windows box)
# ---------------------------------------------------------------------------
function Install-PythonRuntime([string]$InstallDir) {
    $runtimeDir = Join-Path $InstallDir "runtime"
    $pyDir = Join-Path $runtimeDir "python"
    $existing = Join-Path $pyDir "python.exe"
    if (Test-Path -LiteralPath $existing) {
        Write-Host "[install] python runtime already present: $existing (reusing)"
        return $existing
    }
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("codeagent-pbs-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $isInstallerExe = $false
    # v7: where a fresh download lands. Writable package dir ->
    # the cache (canonical name, reused next time / next machine).
    # Read-only or missing package dir -> the temp dir (fail-soft).
    $dlDir = $tmp
    if ((Test-Path -LiteralPath $PackageDir) -and (Test-DirWritable $PackageDir)) {
        $dlDir = $PackageDir
    }
    try {
        if ($PythonArchive) {
            if (-not (Test-Path -LiteralPath $PythonArchive)) {
                throw "[install] -PythonArchive not found: $PythonArchive"
            }
            # v16: a LOCAL archive must pass the SAME verification chain
            # as a cached package-dir copy -- size band + magic, then
            # (per kind) the full check: exe -> Authenticode, tar.gz ->
            # gzip magic + the .sha256 sidecar when one sits next to it.
            # A failed check is a warning + fall through to the NEXT
            # source, exactly like the download path treats a bad
            # mirror. $archive stays $null until something passes.
            $archive = $null
            $ext = [IO.Path]::GetExtension($PythonArchive).ToLower()
            if ($ext -eq ".exe") {
                if (Test-LocalPackage -Path $PythonArchive -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                    $sig = Get-AuthenticodeSignature -FilePath $PythonArchive
                    if ($sig.Status -eq 'Valid' -and $sig.SignerCertificate -and
                        ([string]$sig.SignerCertificate.Subject) -match 'Python Software Foundation') {
                        $archive = $PythonArchive
                        $isInstallerExe = $true
                        Write-Host "[install] python runtime from local archive (Authenticode OK): $PythonArchive"
                    } else {
                        $signer = ""
                        if ($sig.SignerCertificate -and $sig.SignerCertificate.Subject) { $signer = $sig.SignerCertificate.Subject }
                        Write-Warning "[install] -PythonArchive exe signature is '$($sig.Status)' (signer: '$signer') -- will NOT be used, trying the next source. File: $PythonArchive"
                    }
                } else {
                    Write-Warning "[install] -PythonArchive exe failed size-band/MZ magic checks -- will NOT be used, trying the next source. File: $PythonArchive"
                }
            } else {
                # PBS-style install_only.tar.gz
                if (Test-LocalPackage -Path $PythonArchive -ExpectedSize 0 -MagicHex "1F8B") {
                    $sidecar = "$PythonArchive.sha256"
                    $shaOk = $true
                    if (Test-Path -LiteralPath $sidecar) {
                        $want = (Get-Content -LiteralPath $sidecar -Raw).Trim()
                        $have = Get-FileSha256 $PythonArchive
                        if ($want -and $have -ne $want) {
                            $shaOk = $false
                            Write-Warning "[install] -PythonArchive sha256 mismatch (sidecar $sidecar): expected $want, got $have -- will NOT be used, trying the next source"
                        } elseif ($want) {
                            Write-Host "[install] -PythonArchive sha256 OK: $have"
                        }
                    }
                    if ($shaOk) {
                        $archive = $PythonArchive
                        Write-Host "[install] python runtime from local archive: $PythonArchive"
                    }
                } else {
                    Write-Warning "[install] -PythonArchive failed gzip magic check -- will NOT be used, trying the next source. File: $PythonArchive"
                }
            }
            if (-not $archive) {
                $fallback = Join-Path $PackageDir (Get-PackageFileName "PythonInstaller")
                if (Test-LocalPackage -Path $fallback -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                    $sig = Get-AuthenticodeSignature -FilePath $fallback
                    if ($sig.Status -eq 'Valid' -and $sig.SignerCertificate -and
                        ([string]$sig.SignerCertificate.Subject) -match 'Python Software Foundation') {
                        $archive = $fallback
                        $isInstallerExe = $true
                        Write-Host "[install] -PythonArchive rejected, using the cached python installer instead: $fallback (Authenticode OK)"
                    }
                }
            }
        } else {
            $pbs = $null
            if ($Offline) {
                # v16: offline with no (usable) -PythonArchive: ONLY the
                # package-dir cache can serve us -- NO network at all.
                # $archive stays $null when nothing passes verification
                # and the caller prints the full tried-sources error.
                $cachedOffline = Join-Path $PackageDir (Get-PackageFileName "PythonInstaller")
                if (Test-LocalPackage -Path $cachedOffline -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                    $sig = Get-AuthenticodeSignature -FilePath $cachedOffline
                    if ($sig.Status -eq 'Valid' -and $sig.SignerCertificate -and
                        ([string]$sig.SignerCertificate.Subject) -match 'Python Software Foundation') {
                        $archive = $cachedOffline
                        $isInstallerExe = $true
                        $usedLabel = "cached in package dir (offline)"
                        Write-Host "[install] -Offline: using cached python installer from package dir: $cachedOffline (no download, Authenticode OK)"
                    } else {
                        Write-Warning "[install] -Offline: cached python installer in the package dir failed Authenticode -- not usable"
                    }
                }
                if (-not $archive -and $UsePbs) {
                    $pbsOffline = Join-Path $PackageDir (Get-PackageFileName "PbsArchive")
                    if (Test-LocalPackage -Path $pbsOffline -ExpectedSize 0 -MagicHex "1F8B") {
                        $sc = "$pbsOffline.sha256"
                        $ok = $true
                        if (Test-Path -LiteralPath $sc) {
                            $want = (Get-Content -LiteralPath $sc -Raw).Trim()
                            $have = Get-FileSha256 $pbsOffline
                            if ($want -and $have -ne $want) { $ok = $false }
                        } else { $ok = [bool]$AllowUnverified }
                        if ($ok) { $archive = $pbsOffline; Write-Host "[install] -Offline: using cached PBS archive: $pbsOffline" }
                    }
                }
                if (-not $archive) { return $null }
            }
            if ($UsePbs) {
                # v6 OPT-IN: python-build-standalone via npmmirror
                # date-directory layout (the v5 default main path).
                # See Get-PbsArchive for the layout and the hard
                # sha256 requirement. Falls through to the official
                # installer chain when resolution/download fails.
                # v7: package-dir reuse first. A cached PBS tarball
                # with correct gzip magic is used directly; sha256
                # still applies when a manifest is resolvable, and
                # offline reuse without a manifest needs
                # -AllowUnverified (same rule as v5 downloads).
                $pbsPkg = Join-Path $PackageDir (Get-PackageFileName "PbsArchive")
                if (Test-LocalPackage -Path $pbsPkg -ExpectedSize 0 -MagicHex "1F8B") {
                    # sha256 rule for LOCAL reuse: a sidecar
                    # "<pkg>.sha256" (created next to a downloaded PBS
                    # archive by this same script) is verified when
                    # present; without it, offline reuse needs
                    # -AllowUnverified -- same refusal semantics as
                    # v5 manifest-less downloads.
                    $sidecar = "$pbsPkg.sha256"
                    if (Test-Path -LiteralPath $sidecar) {
                        $want = (Get-Content -LiteralPath $sidecar -Raw).Trim()
                        $have = Get-FileSha256 $pbsPkg
                        if ($want -and $have -ne $want) {
                            throw "[install] cached PBS archive sha256 mismatch (sidecar $sidecar): expected $want, got $have -- delete the cached file and re-run"
                        }
                        Write-Host "[install] cached PBS archive sha256 OK: $have"
                    } elseif (-not $AllowUnverified) {
                        Write-Warning "[install] cached PBS archive has no .sha256 sidecar; REFUSING unverified reuse. Re-run with -AllowUnverified, or delete it to force a verified download."
                        $pbsPkg = $null
                    }
                } else { $pbsPkg = $null }
                if ($pbsPkg) {
                    Write-Host "[install] using cached PBS archive from package dir: $pbsPkg"
                    $archive = $pbsPkg
                } else {
                    $pbs = $null
                    try { $pbs = Get-PbsArchive $PbsMirror $PythonVersion } catch { }
                    if (-not $pbs) {
                        # v20: LOUD downgrade notice (was a quiet
                        # Write-Warning the user read past). The
                        # official-installer route WRITES the registry
                        # (HKLM MSI components + HKCU bundle) -- that
                        # is a real behavior change vs the promised
                        # self-contained PBS route, so say it plainly
                        # and give a one-key way out.
                        Write-Host ""
                        Write-Warning "[install] -UsePbs: could NOT resolve a cpython $PythonVersion archive from $PbsMirror (all date directories searched, newest-first)."
                        Write-Warning "[install] DOWNGRADE: falling back to the OFFICIAL INSTALLER path -- this route is NOT self-contained:"
                        Write-Warning "[install]   it writes registry entries (HKLM MSI components + HKCU bundle registration) on this machine."
                        Write-Host   "[install]   To keep the self-contained/no-registry guarantee: stop now (Ctrl+C), pre-seed the package"
                        Write-Host   "[install]   dir ($PackageDir) with cpython-$PythonVersion-...-install_only.tar.gz, or pass -PythonArchive."
                        Write-Host   ""
                    }
                }
            }
            if ($pbs -and -not $archive) {
                Write-Host "[install] -UsePbs: python-build-standalone release $($pbs.Date) ($($pbs.MaskedName))"
                $archive = Join-Path $dlDir (Get-PackageFileName "PbsArchive")
                Write-Host "[install] downloading python runtime: $($pbs.Url)"
                $ok = Save-VerifiedDownload -Url $pbs.Url -Out $archive `
                    -TypeName "gzip tarball" -ContentTypeRegex 'application/(x-)?gzip|application/octet-stream' `
                    -ExpectedSize $pbs.Size -MagicHex "1F8B"
                if ($ok) {
                    # sha256: hard requirement (v5). Checksum and
                    # download come from the SAME mirror directory.
                    # If the manifest is missing or has no matching
                    # line, the download is REFUSED unless
                    # -AllowUnverified.
                    $expected = $pbs.Sha256
                    if ($expected) {
                        $actual = Get-FileSha256 $archive
                        if ($actual -ne $expected) {
                            throw "[install] python archive sha256 mismatch: expected $expected, got $actual (source A: $PbsMirror) -- aborting, this is a hard stop"
                        }
                        Write-Host "[install] python archive sha256 OK: $actual"
                        # v7: write the sidecar so LOCAL package-dir
                        # reuse later can re-verify this checksum.
                        try {
                            [IO.File]::WriteAllText("$archive.sha256", $actual, (New-Object Text.UTF8Encoding($false)))
                        } catch { }
                    } else {
                        if (-not $AllowUnverified) {
                            throw "[install] could not get a sha256 for the python archive from $PbsMirror (SHA256SUMS missing or no matching line). REFUSING to install an unverified binary. Re-run with -AllowUnverified to override, or drop -UsePbs to use the official installer path."
                        }
                        Write-Warning "[install] -AllowUnverified: proceeding WITHOUT a sha256 for the python archive (source A manifest unavailable)"
                    }
                } else {
                    Write-Warning "[install] -UsePbs download failed verification -- falling back to the official installer path"
                    $pbs = $null
                }
            }
            if (-not $archive) {
                # v6 MAIN PATH: official python.org installer exe
                # (PINNED $PythonVersion, default 3.12.10) from
                # Tsinghua, then Aliyun, then a hard error. Per-user
                # install, no admin, no PATH, no .py association, no
                # shortcuts, no test suite.
                # v7: a cached copy in the package dir that passes
                # size band + MZ magic is reused AS-IS (offline
                # move path); the Authenticode signature check below
                # runs on it the same as on a fresh download.
                # Mirrors carry no SHA256SUMS for the exe, so the
                # integrity check is AUTHENTICODE (the installer is
                # signed by the Python Software Foundation).
                $pkgName = Get-PackageFileName "PythonInstaller"
                $cachedPkg = Join-Path $PackageDir $pkgName
                $archive = $null
                if (Test-LocalPackage -Path $cachedPkg -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                    $archive = $cachedPkg
                    $usedLabel = "cached in package dir"
                    Write-Host "[install] using cached python installer from package dir: $cachedPkg (no download)"
                } else {
                    $sources = @(
                        @{ Label = "Tsinghua"; Url = ($PythonInstallerMirror.TrimEnd('/') + "/$PythonVersion/$pkgName") }
                        @{ Label = "Aliyun";   Url = ($PythonInstallerMirror2.TrimEnd('/') + "/$pkgName") }
                    )
                    $ok = $false
                    $usedLabel = ""
                    foreach ($src in $sources) {
                        $outPath = Join-Path $dlDir $pkgName
                        Write-Host "[install] official installer source $($src.Label): downloading $($src.Url)"
                        if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force -ErrorAction SilentlyContinue }
                        if (Save-VerifiedDownload -Url $src.Url -Out $outPath `
                            -TypeName "official python installer exe" -ContentTypeRegex 'application/(octet-stream|x-ms-dos-executable|msdownload|x-msdownload)' `
                            -ExpectedSize $Script:PyInstallerBytes -MagicHex "4D5A") {
                            $ok = $true; $usedLabel = $src.Label; $archive = $outPath; break
                        }
                        Write-Warning "[install] $($src.Label) failed (content-type / size band 26.5-27.5MB / MZ magic) -- trying the next source"
                    }
                    if (-not $ok) {
                        throw @"
[install] python installer download FAILED on all mirrors ($(($sources | ForEach-Object { $_.Label }) -join ', ')). Options:
  (1) pre-seed the package dir ($PackageDir) with $pkgName and re-run (a cached copy passing magic+size+signature is used without any download)
  (2) pass -PythonArchive C:\path\to\cpython-...-install_only.tar.gz (a python-build-standalone install_only build)
  (3) install Python $PythonVersion yourself (https://www.python.org/downloads/) and re-run with -PythonPath
  (4) retry later / check the network
"@
                    }
                }
                # Authenticode signature: the real integrity check.
                # Official installers are signed by "Python Software
                # Foundation". An unsigned/tampered exe -> refuse.
                $sig = Get-AuthenticodeSignature -FilePath $archive
                if ($sig.Status -ne 'Valid') {
                    throw "[install] python installer Authenticode signature is '$($sig.Status)' (expected Valid). REFUSING to run a possibly tampered installer from $usedLabel. File: $archive"
                }
                $signer = ""
                if ($sig.SignerCertificate -and $sig.SignerCertificate.Subject) { $signer = $sig.SignerCertificate.Subject }
                if ($signer -notmatch 'Python Software Foundation') {
                    throw "[install] python installer is signed by '$signer', expected 'Python Software Foundation'. REFUSING to run it. File: $archive"
                }
                Write-Host "[install] Authenticode OK: $signer ($($sig.Status), source: $usedLabel)"
                $isInstallerExe = $true
            }
        }
        # v16: offline with NO usable archive anywhere (explicit archive
        # rejected, nothing usable in the package dir) -> signal the
        # caller (which prints the full tried-sources error). The
        # online path guarantees $archive or a throw above.
        if (-not $archive) { return $null }
        if ($isInstallerExe) {
            # v10: BEFORE running the installer, check whether this
            # exact version is already REGISTERED on the machine.
            # The official installer (WiX bundle) is a per-version
            # single instance: with WixBundleInstalled=1 a /quiet run
            # exits 0 WITHOUT installing anything (TargetDir silently
            # ignored) -- and a DANGLING registration (files deleted)
            # produces the same silent skip via Detect: ... Present.
            $regCheck = Test-PinnedPythonRegistration $PythonVersion ([bool]$ForceOwnRuntime)
            foreach ($src in $regCheck.Sources) {
                Write-Host "[install] pinned $PythonVersion registration trace: $src"
            }
            if ($regCheck.Action -eq "reuse") {
                # v18: an explicitly supplied -PythonArchive WINS over
                # registry reuse (the stageE field incident: reuse bound
                # the venv to a foreign directory and the user's
                # self-contained archive was never used). Fall through
                # and run the archive. The signature check above has
                # ALREADY accepted this $archive as the PSF-signed
                # official installer, so the machinery below (this
                # registration check included) applies to it as-is.
                if ($PythonArchive -and ($archive -eq $PythonArchive)) {
                    Write-Warning "[install] -PythonArchive was given ($PythonArchive) -- it takes priority over the registered Python $PythonVersion at $($regCheck.Exe): the archive will be installed under <InstallDir>\runtime\python instead of REUSING the registry entry."
                } else {
                # Registration healthy and it IS the pinned version:
                # reuse it as the venv base. No download, no
                # installer run (a second copy is impossible anyway).
                Write-Host "[install] detected an already-installed Python $PythonVersion at $($regCheck.Exe) -- REUSING it (no download, no installer run)"
                return $regCheck.Exe
                }
            }
            if ($regCheck.Action -eq "broken") {
                # v20: no longer a dead end (field 2026-10-13: /repair
                # -> 1602 AND /uninstall -> 1603 -- the published recipe
                # did not work, and the installer just stopped). The
                # broken registration only blocks the OFFICIAL-INSTALLER
                # route (it would silently skip: WixBundleInstalled /
                # Detect: Present). Every other route is unaffected:
                # -UsePbs and a PBS/PythonArchive tarball write no
                # registration at all. So: warn loudly, then fall
                # through -- if the user gave -UsePbs / -PythonArchive
                # the PBS path above already set $archive (non-exe)
                # and this whole block is skipped; if they gave
                # -IgnoreRegistryState we skip the gate entirely;
                # only the plain official-installer route still stops
                # (it genuinely cannot proceed), with the registry
                # GUID-level removal recipe appended.
                if ($IgnoreRegistryState) {
                    Write-Warning "[install] -IgnoreRegistryState: pinned $PythonVersion registration is broken/dangling but the gate is SKIPPED by explicit request -- the registry is not read further and not written by this switch."
                } elseif ($UsePbs) {
                    # v20: -UsePbs was requested (the PBS resolution failed and
                    # we fell back to the official installer above, LOUDLY).
                    # Do not dead-end here: the official installer may still
                    # succeed (this machine's registration belongs to the
                    # same broken trace, WiX may repair-over); if it silently
                    # skips, the v10 post-install exe check fails LOUDLY with
                    # the installer log dumped -- nothing is half-installed.
                    Write-Warning "[install] -UsePbs was requested but the PBS archive could not be resolved (see the DOWNGRADE notice above); continuing on the official-installer route despite the broken registration. If the installer silently skips, the post-install check will fail loudly with the installer log."
                } elseif ($PythonArchive -and $archive) {
                    Write-Warning "[install] a self-contained archive route is selected ($archive) -- it writes no registration, the broken state does not block it. Continuing."
                } else {
                    Write-Warning "[install] Python $PythonVersion is REGISTERED on this machine but broken (interpreter missing or not runnable at '$($regCheck.Exe)') -- the OFFICIAL INSTALLER route would silently skip (WixBundleInstalled / Detect: Present)."
                    throw @"
[install] Python $PythonVersion registration is broken AND no alternative route was selected. FIX (pick one), then re-run:
  (1) -UsePbs            -- portable python-build-standalone runtime (npmmirror, sha256-verified): NO registration read or written, works around the dangling state entirely
  (2) -IgnoreRegistryState -- skip the registration gate (the official installer runs anyway; if it still silently-skips, the install fails loudly at the post-install exe check -- nothing is half-installed)
  (3) -PythonArchive C:\path\to\cpython-...-install_only.tar.gz -- self-contained archive route, no registration involved
  (4) -PythonPath C:\path\to\python.exe -- reuse an existing interpreter
  (5) clean the registration for real (the /repair+/uninstall recipe FAILED on the field box with 1602/1603; the reliable route is GUID-level removal):
      msiexec /x {GUID} /quiet  -- one per MSI component, GUIDs read from
        HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ (DisplayName like 'Python 3.12 Core Interpreter')
        and HKCU\Software\Classes\Installer\Dependencies (each child key name IS the dependency GUID)
      then delete the leftover HKCU\Software\Python\PythonCore\3.12 tree
"@
                }
            }
            if ($regCheck.Action -eq "conflict") {
                throw @"
[install] -ForceOwnRuntime asked for a private Python $PythonVersion copy, but this version is already registered on this machine (sources: $($regCheck.Sources -join '; ')).
The official installer CANNOT install a second copy of the same version (per-version single instance) -- it would silently exit 0.
FIX (pick one):
  (1) re-run WITHOUT -ForceOwnRuntime (the registered interpreter will be reused automatically)
  (2) -UsePbs for a fully private portable copy (no registration involved)
  (3) -PythonVersion 3.12.9 (a patch version not yet taken)
"@
            }
            # regCheck.Action == "clear": safe to run the installer.
            # Official installer, silent and fully private: per-user,
            # no PATH, no .py association, no shortcuts, no tests,
            # no admin. TargetDir pins it inside the app tree.
            # v10: ALWAYS log to the package dir when writable (else
            # temp) so a silent-skip failure is diagnosable after
            # the fact -- WixBundleInstalled / Detect states / Exit
            # code are what located this exact root cause.
            $target = Join-Path $runtimeDir "python"
            $logDir = $tmp
            if ((Test-Path -LiteralPath $PackageDir) -and (Test-DirWritable $PackageDir)) { $logDir = $PackageDir }
            $installerLog = Join-Path $logDir "python-installer.log"
            Write-Host "[install] running python installer (quiet, per-user, no PATH, no association) into $target (log: $installerLog)"
            & $archive /quiet InstallAllUsers=0 "TargetDir=$target" PrependPath=0 Include_launcher=0 `
                AssociateFiles=0 Shortcuts=0 Include_test=0 Include_pip=1 "/log=$installerLog" | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Show-PythonInstallerLog $installerLog
                throw "[install] python installer failed (rc=$LASTEXITCODE, see the LOG lines above from $installerLog). If they show WixBundleInstalled=1 or Detect ... Present, a same-version registration already exists -- see the repair recipe in the error above / the README. Otherwise try -PythonArchive, a different mirror via -PythonInstallerMirror, or install Python $PythonVersion yourself and pass -PythonPath."
            }
            $exe = Join-Path $pyDir "python.exe"
            if (-not (Test-Path -LiteralPath $exe)) {
                # v10: exit 0 + missing exe is EXACTLY the silent-skip
                # signature. Dump the installer log before dying so
                # the user sees WixBundleInstalled / Detect / Exit.
                Show-PythonInstallerLog $installerLog
                throw "[install] python installer reported success but $exe is missing (unexpected layout). The LOG lines above come from $installerLog -- if they show WixBundleInstalled=1 or Detect ... Present, the version was already registered and the installer skipped; run 'python-$PythonVersion-amd64.exe /repair /quiet' then '/uninstall /quiet' (or clean the registry) and re-run."
            }
            $ver = $null
            try { $ver = (& $exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])") } catch { }
            if (-not $ver) { throw "[install] installed python.exe failed to run a trivial -c command" }
            if ($ver -ne $PythonVersion) {
                throw "[install] installed Python is $ver, expected the PINNED $PythonVersion -- aborting (wrong installer somehow ran)"
            }
            $pipVer = $null
            try { $pipVer = (& $exe -m pip --version) } catch { }
            if (-not $pipVer) { throw "[install] installed python has no working pip (Include_pip=1 should have laid it in) -- aborting" }
            Write-Host "[install] python runtime installed: $exe (Python $ver, official installer, pip OK)"
            return $exe
        }
        # 4) extract: install_only tar.gz contains a single 'python/'
        #    directory with python.exe at its top. Win10 1803+ ships
        #    bsdtar as tar.exe; fall back to "tar" on PATH.
        $destRoot = Join-Path $tmp "extract"
        New-Item -ItemType Directory -Path $destRoot -Force | Out-Null
        $tar = "tar"
        if (Get-Command tar -ErrorAction SilentlyContinue) { $tar = (Get-Command tar).Source }
        if (-not $tar -or $tar -eq "tar") {
            # "tar" fallback only when the command actually resolves
            if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
                throw "[install] tar not found -- needed to extract the python runtime (Win10 1803+ ships bsdtar; install it or use the .exe installer route)"
            }
        }
        & $tar -xzf $archive -C $destRoot
        if ($LASTEXITCODE -ne 0) { throw "[install] tar extract failed for python archive (rc=$LASTEXITCODE)" }
        $inner = Join-Path $destRoot "python"
        if (-not (Test-Path -LiteralPath (Join-Path $inner "python.exe"))) {
            throw "[install] extracted archive did not contain python\python.exe (unexpected layout)"
        }
        # 5) move into <InstallDir>\runtime\python
        if (Test-Path -LiteralPath $pyDir) { Remove-Item -LiteralPath $pyDir -Recurse -Force }
        Move-Item $inner $pyDir
        $exe = Join-Path $pyDir "python.exe"
        # 6) smoke-test the interpreter before trusting it
        $ver = $null
        try { $ver = (& $exe -c "import sys; print('%d.%d' % sys.version_info[:2])") } catch { }
        if (-not $ver) { throw "[install] bootstrapped python.exe failed to run a trivial -c command" }
        Write-Host "[install] python runtime bootstrapped: $exe (Python $ver)"
        return $exe
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Install-NodeRuntime([string]$InstallDir) {
    $nodeDir = Join-Path $InstallDir "runtime\node"
    $existing = Join-Path $nodeDir "node.exe"
    if (Test-Path -LiteralPath $existing) {
        Write-Host "[install] node runtime already present: $existing (reusing)"
        return $existing
    }
    $runtimeDir = Join-Path $InstallDir "runtime"
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("codeagent-node-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    # v7: fresh downloads land in the package dir when writable
    # (canonical name -> reused on this machine and others).
    $dlDir = $tmp
    if ((Test-Path -LiteralPath $PackageDir) -and (Test-DirWritable $PackageDir)) {
        $dlDir = $PackageDir
    }
    try {
        if ($NodeArchive) {
            if (-not (Test-Path -LiteralPath $NodeArchive)) {
                throw "[install] -NodeArchive not found: $NodeArchive"
            }
            # v16: a LOCAL node zip must pass the SAME verification chain
            # as a cached/downloaded one -- zip magic + size band, then
            # sha256 from the SHASUMS256 sidecar when one is available
            # (the package dir's node-<v>-SHASUMS256.txt, written next to
            # a downloaded zip by this script, or "<archive>.sha256").
            # Verification failure -> warn, treat as absent, fall through
            # to the package-dir cache / (online) the download chain.
            $nodeArchiveOk = $false
            $nodeArchiveSha = $null
            if (Test-LocalPackage -Path $NodeArchive -ExpectedSize $Script:NodeZipBytes -MagicHex "504B0304") {
                $nodeArchiveOk = $true
                $naName = [IO.Path]::GetFileName($NodeArchive)
                $sumsSidecar = Join-Path $PackageDir (Get-PackageFileName "NodeSums")
                foreach ($sc in @($sumsSidecar, "$NodeArchive.sha256")) {
                    if ($nodeArchiveSha) { break }
                    if (-not (Test-Path -LiteralPath $sc)) { continue }
                    foreach ($line in ((Get-Content -LiteralPath $sc) -split "`n")) {
                        $line = $line.Trim("`r")
                        if ($line -like "*$naName") { $nodeArchiveSha = ($line -split '\s+')[0]; break }
                    }
                }
                if ($nodeArchiveSha) {
                    $have = Get-FileSha256 $NodeArchive
                    if ($have -ne $nodeArchiveSha) {
                        Write-Warning "[install] -NodeArchive sha256 mismatch: expected $nodeArchiveSha, got $have -- will NOT be used, trying the next source. File: $NodeArchive"
                        $nodeArchiveOk = $false
                    } else {
                        Write-Host "[install] -NodeArchive sha256 OK: $have"
                    }
                } else {
                    Write-Warning "[install] -NodeArchive has no SHASUMS256 source to verify against (no sidecar next to it or in the package dir) -- using it with magic+size checks only"
                }
            } else {
                Write-Warning "[install] -NodeArchive failed zip magic / size-band checks -- will NOT be used, trying the next source. File: $NodeArchive"
            }
            if ($nodeArchiveOk) {
                $archive = $NodeArchive
                Write-Host "[install] node runtime from local archive: $NodeArchive"
                # Node zip layout is fixed: node-vX.Y.Z-win-x64\node.exe
            }
        } else {
            # 1) v6: version is PINNED (default v22.20.0, live-verified
            #    on npmmirror 2026-10-10). No index.tab resolution and
            #    no "latest" -- dynamic resolution was the old default.
            $nodeVer = $NodeVersion
            if ($nodeVer -notmatch '^v') { $nodeVer = "v$nodeVer" }
            $fname = Get-PackageFileName "NodeZip"
            $dlUrl = ($NodeMirror.TrimEnd('/') + "/" + $nodeVer + "/" + $fname)
            # 2) expected sha256: v7 first from the package dir's
            #    cached SHASUMS sidecar (written when the zip was
            #    downloaded), then from the live SHASUMS256.txt
            #    (same mirror directory as the download). Hard
            #    requirement: no checksum -> REFUSE unless
            #    -AllowUnverified.
            $expected = $null
            $sumsSidecar = Join-Path $PackageDir (Get-PackageFileName "NodeSums")
            if (Test-Path -LiteralPath $sumsSidecar) {
                foreach ($line in ((Get-Content -LiteralPath $sumsSidecar) -split "`n")) {
                    $line = $line.Trim("`r")
                    if ($line -like "*$fname") { $expected = ($line -split '\s+')[0]; break }
                }
            }
            if (-not $expected) {
                try {
                    $sums = (Invoke-WebRequest -UseBasicParsing -Uri ($NodeMirror.TrimEnd('/') + "/" + $nodeVer + "/SHASUMS256.txt")).Content
                    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
                    foreach ($line in ($sums -split "`n")) {
                        $line = $line.Trim("`r")
                        if ($line -like "*$fname") { $expected = ($line -split '\s+')[0]; break }
                    }
                    # v7: cache the manifest in the package dir so
                    # OFFLINE reuse still has a checksum to verify.
                    if ($expected -and $dlDir -eq $PackageDir) {
                        try { [IO.File]::WriteAllText($sumsSidecar, $sums, (New-Object Text.UTF8Encoding($false))) } catch { }
                    }
                } catch { }
            }
            # 3) v7 package-dir reuse: a cached zip passing size band
            #    + zip magic is used directly (offline move path);
            #    the sha256 check below applies to it exactly as to
            #    a fresh download.
            $archive = Join-Path $PackageDir $fname
            $cached = $false
            if (Test-LocalPackage -Path $archive -ExpectedSize $Script:NodeZipBytes -MagicHex "504B0304") {
                $cached = $true
                Write-Host "[install] using cached node zip from package dir: $archive (no download)"
            } elseif ($Offline) {
                # v16: -Offline never downloads; no verified local zip
                # (package dir empty / archive rejected) -> fail (the
                # caller treats node as fail-soft and warns).
                throw "[install] -Offline: no usable node zip locally (package dir has none, -NodeArchive rejected or absent) -- skipping node bootstrap. Copy $(Get-PackageFileName "NodeZip") (+ $(Get-PackageFileName "NodeSums")) into $PackageDir or pass -NodeArchive."
            } else {
                # download with hard verification (content-type /
                #    pinned size band 34-37 MB / zip magic PK\x03\x04).
                $archive = Join-Path $dlDir $fname
                Write-Host "[install] downloading node runtime (pinned $nodeVer): $dlUrl"
                $ok = Save-VerifiedDownload -Url $dlUrl -Out $archive `
                    -TypeName "zip archive" -ContentTypeRegex 'application/(x-)?zip|application/octet-stream' `
                    -ExpectedSize $Script:NodeZipBytes -MagicHex "504B0304"
                if (-not $ok) {
                    # 3 tries, then hard failure (node is fail-soft at the
                    # CALLER level, but a corrupt file must never reach
                    # Expand-Archive).
                    $downloaded = $false
                    for ($i = 2; $i -le 3; $i++) {
                        Write-Warning "[install] node download attempt $i (bad content-type / size / magic bytes last time)"
                        if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
                        if (Save-VerifiedDownload -Url $dlUrl -Out $archive `
                            -TypeName "zip archive" -ContentTypeRegex 'application/(x-)?zip|application/octet-stream' `
                            -ExpectedSize $Script:NodeZipBytes -MagicHex "504B0304") { $downloaded = $true; break }
                    }
                    if (-not $downloaded) {
                        throw "[install] node download kept failing verification (not a real zip): $dlUrl -- pass -NodeArchive or -NoNode"
                    }
                }
            }
            # 4) sha256: hard requirement (v5). Refuse when the
            #    manifest is missing, unless -AllowUnverified.
            if ($expected) {
                $actual = Get-FileSha256 $archive
                if ($actual -ne $expected) {
                    throw "[install] node archive sha256 mismatch: expected $expected, got $actual -- aborting (hard stop)"
                }
                Write-Host "[install] node archive sha256 OK: $actual"
            } else {
                if (-not $AllowUnverified) {
                    throw "[install] could not get a sha256 for $fname from $NodeMirror or the package dir (SHASUMS256.txt/sidecar missing or no matching line). REFUSING to install an unverified binary. Re-run with -AllowUnverified, pass -NodeArchive, pre-seed the package dir with a verified $fname + sidecar, or use -NoNode (the app does not need Node at runtime)."
                }
                Write-Warning "[install] -AllowUnverified: proceeding WITHOUT a sha256 for the node archive"
            }
        }
        # 4) extract zip (PS 5.1: Expand-Archive; fine for zips)
        $destRoot = Join-Path $tmp "extract"
        Expand-Archive -Path $archive -DestinationPath $destRoot -Force
        $inner = Get-ChildItem -Directory $destRoot | Select-Object -First 1
        if (-not $inner -or -not (Test-Path -LiteralPath (Join-Path $inner.FullName "node.exe"))) {
            throw "[install] extracted node archive did not contain node.exe (unexpected layout)"
        }
        # 5) move into <InstallDir>\runtime\node
        if (Test-Path -LiteralPath $nodeDir) { Remove-Item -LiteralPath $nodeDir -Recurse -Force }
        Move-Item $inner.FullName $nodeDir
        $exe = Join-Path $nodeDir "node.exe"
        $out = $null
        try { $out = (& $exe --version) } catch { }
        if (-not $out) { throw "[install] bootstrapped node.exe failed to run --version" }
        Write-Host "[install] node runtime bootstrapped: $exe ($out)"
        return $exe
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

# v16: offline wrapper. Install-PythonRuntime's normal entry reuses an
# existing runtime or throws; offline callers need a NULL return when
# no local source works (the caller prints the tried-sources error).
function Invoke-AppBuild([string]$InstallDir, [string]$NodeExe) {
    # v24 (t_ab909828): the app's launch-critical artifacts are BUILD
    # PRODUCTS, not repo content: .gitignore excludes dist/ (line 2)
    # and electron/ui/ (line 26), so a clone/zip source has neither.
    # electron/main.ts loads electron/ui/index.html and the CodeAgent.bat
    # launcher needs dist/electron/main.js -- without a build the app
    # opens to a black window. This function makes the installer
    # produce them itself. Uses the install tree's OWN Node
    # (<InstallDir>\runtime\node -- prepended to PATH), never a system
    # Node. npm registry / electron binary both pinned to npmmirror.
    # $NodeExe may be $null (NoNode / failed bootstrap): then try PATH
    # as a fallback; if that also has no node, we throw.
    $nodeDir = $null
    if ($NodeExe -and (Test-Path -LiteralPath $NodeExe)) {
        $nodeDir = Split-Path -Parent $NodeExe
        $env:PATH = "$nodeDir;$env:PATH"
    } elseif (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw @"
[install] build needs Node/npm but none is available.
FIX (pick one):
  - re-run WITHOUT -NoNode so the installer bootstraps its own Node into <InstallDir>\runtime\node
  - lay node.exe yourself at <InstallDir>\runtime\node\node.exe and re-run
  - pass -SkipBuild (the installed app will NOT start until you build it manually)
"@
    }
    $npmCmd = (Get-Command npm -ErrorAction SilentlyContinue)
    if ($npmCmd) { Write-Host "[install] build using npm from: $($npmCmd.Source)" }

    $env:npm_config_registry = "https://registry.npmmirror.com"
    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"

    # 1) npm install at the install root (deps + electron binary).
    #    Skipped when node_modules\electron is already there AND the
    #    prior copy verified -- the electron binary download is the
    #    slow part and a copied one is already complete.
    $electronExe = Join-Path $InstallDir "node_modules\electron\dist\electron.exe"
    if (Test-Path -LiteralPath $electronExe) {
        Write-Host "[install] build: node_modules\electron\dist\electron.exe already present -- skipping 'npm install' at root"
    } else {
        Invoke-Step "npm install (app root; registry=npmmirror, ELECTRON_MIRROR set)" {
            Push-Location $InstallDir
            try {
                npm install --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { throw "[install] npm install failed at app root (rc=$LASTEXITCODE) -- no network? try -Offline + -SkipBuild or fix the mirror" }
            } finally { Pop-Location }
            $global:LASTEXITCODE = 0
        }
    }

    # 2) npm run build -> dist/electron/*.js + electron/renderer.bundle.js
    $mainJs = Join-Path $InstallDir "dist\electron\main.js"
    if ((Test-Path -LiteralPath $mainJs) -and (Test-Path -LiteralPath (Join-Path $InstallDir "electron\renderer.bundle.js"))) {
        Write-Host "[install] build: dist\electron\main.js + electron\renderer.bundle.js already present -- skipping 'npm run build'"
    } else {
        Invoke-Step "npm run build (tsc + esbuild -> dist\electron\*.js + electron\renderer.bundle.js)" {
            Push-Location $InstallDir
            try {
                npm run build
                if ($LASTEXITCODE -ne 0) { throw "[install] npm run build failed (rc=$LASTEXITCODE)" }
            } finally { Pop-Location }
            $global:LASTEXITCODE = 0
        }
    }

    # 3) build the UI and stage ui/dist -> electron/ui (the dir the
    #    main process actually loads). scripts\build-ui.ps1 does:
    #    npm --prefix ui install; npm --prefix ui run build
    #    (tsc -b + vite build); stage; blank .env / config.json (both
    #    exist already post-install, so it leaves them untouched).
    $uiIndex = Join-Path $InstallDir "electron\ui\index.html"
    if (Test-Path -LiteralPath $uiIndex) {
        Write-Host "[install] build: electron\ui\index.html already present -- skipping scripts\build-ui.ps1"
    } else {
        $buildUi = Join-Path $InstallDir "scripts\build-ui.ps1"
        if (-not (Test-Path -LiteralPath $buildUi)) { throw "[install] build: scripts\build-ui.ps1 not found under the install tree -- source copy incomplete?" }
        Invoke-Step "building the UI (scripts\build-ui.ps1 -> electron\ui)" {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $buildUi
            if ($LASTEXITCODE -ne 0) { throw "[install] build-ui.ps1 failed (rc=$LASTEXITCODE) -- UI will be missing; the app window would open BLACK" }
            $global:LASTEXITCODE = 0
        }
    }
}

function Install-PythonRuntimeOffline([string]$InstallDir) {
    $runtimeDir = Join-Path $InstallDir "runtime"
    $pyDir = Join-Path $runtimeDir "python"
    $existing = Join-Path $pyDir "python.exe"
    if (Test-Path -LiteralPath $existing) {
        Write-Host "[install] python runtime already present: $existing (reusing)"
        return $existing
    }
    return (Install-PythonRuntime $InstallDir)
}

function Invoke-Step([string]$Label, [scriptblock]$Body) {
    Write-Host "[install] $Label"
    & $Body
}

# v16: clean recursive delete for -PurgeData (the field failure: an
# UnauthorizedAccessException on a read-only-attribute file spewed a
# dirty error mid-delete even though the delete eventually "worked").
# Discipline: strip read-only/compressed/hidden/system attributes
# first, retry locked files up to 3 times (spread waits -- the lock
# holder, e.g. a just-killed electron, needs a moment), per-item
# failures log a WARNING only, and a final re-check decides COMPLETE
# vs INCOMPLETE.
function Remove-DirectoryClean([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    # 1) strip blocking attributes on everything below (and the root)
    try {
        Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object { -not $_.PSIsContainer } |
            ForEach-Object {
                if ($_.Attributes -band [IO.FileAttributes]::ReadOnly) {
                    try { $_.Attributes = $_.Attributes -bor [IO.FileAttributes]::Normal } catch { }
                }
            }
        $rootItem = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($rootItem -and ($rootItem.Attributes -band [IO.FileAttributes]::ReadOnly)) {
            try { $rootItem.Attributes = [IO.FileAttributes]::Directory } catch { }
        }
    } catch { }
    # 2) delete with retries; collect the items that keep failing
    $leftovers = @()
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if ($attempt -gt 1) { Start-Sleep -Milliseconds (500 * $attempt) }
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            $leftovers = @()
            break
        } catch {
            if ($attempt -eq 3) {
                $leftovers = @()
                if (Test-Path -LiteralPath $Path) {
                    try { $leftovers = @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue | Select-Object -First 10 -ExpandProperty FullName) } catch { }
                    if ($leftovers.Count -eq 0) { $leftovers = @($Path) }
                }
            } else {
                Write-Warning ("[uninstall] delete attempt {0} failed ({1}) -- retrying" -f $attempt, $_.Exception.Message)
            }
        }
    }
    # 3) per-item warnings (non-fatal), then the hard verdict
    foreach ($item in $leftovers) {
        Write-Warning "[uninstall] could not delete: $item"
    }
    if (Test-Path -LiteralPath $Path) {
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# uninstall path
# ---------------------------------------------------------------------------
if ($Uninstall) {
    if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA $Script:AppDirName }
    $InstallDir = [Environment]::ExpandEnvironmentVariables($InstallDir)
    # v13: default PackageDir BEFORE first use -- the param-defaulting
    # block below this `exit` only serves the install path, so on a
    # bare -Uninstall $PackageDir was "" (and Test-Path -LiteralPath ''
    # THROWS on PS 5.1 -- that PS 5.1 crash).
    if (-not $PackageDir) { $PackageDir = Join-Path $InstallDir "packages" }
    else { $PackageDir = Resolve-PathSafe $PackageDir }
    Write-Host "[uninstall] removing: $InstallDir"
    # v17: read the install-time data-dir record BEFORE the install
    # dir is deleted -- the purge gate below needs it as ownership
    # proof (b)/(c) when the data dir itself carries no marker.
    $Script:UninstallRecordedDataDir = ""
    $Script:UninstallPurgeFailed = $false
    $recPath = Join-Path $InstallDir ".install-state.json"
    if (Test-PathSafe $recPath) {
        try {
            $rec = Get-Content -LiteralPath $recPath -Raw | ConvertFrom-Json -ErrorAction Stop
            $Script:UninstallRecordedDataDir = [string]$rec.dataDir
        } catch { }
    }
    if ($Script:UninstallRecordedDataDir.Trim()) {
        Write-Host "[uninstall] install record says data dir was: $Script:UninstallRecordedDataDir"
    }
    # v25 (t_27714fcb): run the -PurgeData gate BEFORE the install
    # tree is deleted. When the data dir lives OUTSIDE the InstallDir
    # (explicit -DataDir, or a legacy %APPDATA% install) this changes
    # nothing: the gate resolves, proves ownership, purges or refuses,
    # exactly as before. When the data dir lives INSIDE the InstallDir
    # (the v25 default), skipping -PurgeData no longer silently eats
    # the data with the tree: the tree delete below removes everything
    # INCLUDING <InstallDir>\data (the whole directory IS ours -- same
    # ownership the v22 marker already proves), but a -PurgeData run
    # first still handles the explicit/external-dir cases explicitly.
    if ($PurgeData) {
        $purgeOk = Invoke-PurgeData $DataDir $InstallDir ([bool]$Force)
        if (-not $purgeOk) { $Script:UninstallPurgeFailed = $true }
    } else {
        $dataResolved = Resolve-DataDir $DataDir $InstallDir
        Write-Host ("[uninstall] data dir kept: {0} (pass -PurgeData to delete it; in-tree data dirs under the install dir are removed with the tree)" -f $dataResolved.DataDir)
    }
    if (Test-PathSafe $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
    else { Write-Warning "[uninstall] install dir not found: $InstallDir (nothing to remove)" }

    # v11/v13: the official installer's OWN WiX registration (ARP entry
    # + Installer\Dependencies keys) is what makes the NEXT
    # same-version install silently skip (WixBundleInstalled). We
    # remove it by running the official installer itself:
    # /uninstall /quiet, with /repair /quiet first when the direct
    # uninstall fails (field-verified: files-deleted state ->
    # /uninstall 1603; /repair 0; /uninstall 0). OWNERSHIP (v13):
    # ONLY a PythonCore\<tag>\InstallPath pointing INSIDE <InstallDir>
    # marks the registration as ours (the per-user installer never
    # writes an ARP InstallLocation, so that field cannot gate
    # anything). When ours, the installer chain removes PythonCore +
    # ARP + WiX Dependencies together. When not ours, nothing is
    # touched and the installer is never run against it.
    $regCleaned = $true
    $ownershipWasOurs = $false
    try {
        $ownership = Test-RegistrationIsOurs $PythonVersion $InstallDir
        $ownershipWasOurs = [bool]$ownership.Ours
        foreach ($line in $ownership.Report) {
            Write-Host "[uninstall] registration surface: $line"
        }
        if ($ownership.Ours) {
            # log dir: package dir when writable (it outlives the
            # install dir we just deleted), else temp.
            $logDir = Join-Path ([IO.Path]::GetTempPath()) ("codeagent-uninstall-" + [Guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
            if ((Test-PathSafe $PackageDir) -and (Test-DirWritable $PackageDir)) { $logDir = $PackageDir }
            $installerExe = $null
            $regOk = $false
            try {
                $installerExe = Get-PythonInstallerExe $logDir
                $regOk = Remove-OfficialPythonRegistration $installerExe $logDir
            } catch {
                Write-Warning "[uninstall] $($_.Exception.Message)"
                $regOk = $false
            }
            if (-not $regOk) { $regCleaned = $false }
            # /repair may have re-created files under our (deleted)
            # install dir -- strip them again so the uninstall truly
            # leaves nothing behind (we already unconditionally
            # removed InstallDir above, so this is consistent).
            if (Test-PathSafe $InstallDir) {
                Write-Host "[uninstall] /repair re-created $InstallDir -- removing it again"
                Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        } else {
            Write-Host "[uninstall] no $PythonVersion PythonCore InstallPath points inside $InstallDir -- the official installer was NOT run (a registration that is not ours stays put)"
        }
    } catch {
        Write-Warning "[uninstall] could not evaluate/clean the official installer registration: $($_.Exception.Message)"
        $regCleaned = $false
    }

    # v11: POST-UNINSTALL VERIFICATION. The three registration
    # surfaces for the pinned version (PythonCore / ARP / WiX
    # Dependencies) must ALL be 0 now. Any residue -> hard failure
    # with a manual-cleanup recipe (the next same-version install
    # would silently skip otherwise).
    $exitCode = 0
    try {
        $residue = Get-RegistrationResidue $PythonVersion
        Write-Host ("[uninstall] registration residue after cleanup: PythonCore={0}, ARP={1}, WiX Dependencies={2} (all must be 0 when the registration was ours)" -f $residue.PythonCore, $residue.ARP, $residue.Deps)
        if ($residue.Total -gt 0 -and $ownershipWasOurs) {
            $regCleaned = $false
        } elseif ($residue.Total -gt 0) {
            # not ours: the residue belongs to the user's own same-
            # version Python; we deliberately left it untouched.
            Write-Host "[uninstall] residue above belongs to a registration that is NOT ours (left untouched by design) -- not a failure"
        }
    } catch {
        Write-Warning "[uninstall] could not verify the registration residue: $($_.Exception.Message)"
        $regCleaned = $false
    }
    if (-not $regCleaned) {
        Write-Warning @"
[uninstall] INCOMPLETE: the official Python $PythonVersion registration was not fully removed. Leftover entries will make the NEXT same-version install SILENTLY SKIP (WixBundleInstalled). Manual cleanup:
  - ARP entries with DisplayName 'Python $PythonVersion': HKCU/HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall (and WOW6432Node)
  - HKCU\Software\Python\PythonCore\<$(Get-VersionKey $PythonVersion)>
  - HKCU\Software\Classes\Installer\Dependencies (Python $PythonVersion * keys)
Or run it yourself: python-$PythonVersion-amd64.exe /repair /quiet, then python-$PythonVersion-amd64.exe /uninstall /quiet
"@
        $exitCode = 1
    }

    $startMenu = Join-Path $env:APPDATA ("Microsoft\Windows\Start Menu\Programs")
    foreach ($lnk in @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) "$($Script:AppDirName).lnk"),
        (Join-Path $startMenu "$($Script:AppDirName).lnk")
    )) {
        if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Host "[uninstall] removed $lnk" }
    }
    # v25 (t_27714fcb): the -PurgeData gate moved ABOVE the install-dir
    # delete (it needs the .install-state.json record + the in-tree
    # data dir to still exist). This is only the failure aggregation.
    if ($Script:UninstallPurgeFailed) {
        Write-Host "[uninstall] done WITH ERRORS -- the data dir purge was refused or incomplete (see the REFUSED/INCOMPLETE warning above)."
        $exitCode = 1
    }
    if ($exitCode -eq 0) {
        Write-Host "[uninstall] done."
    } else {
        Write-Host "[uninstall] done WITH ERRORS -- see the INCOMPLETE warning above."
    }
    exit $exitCode
}

# ---------------------------------------------------------------------------
# interactive default (no args at all -> prompt). Skipped for -CheckOnly:
# a dry-run must not prompt; it checks the DEFAULT target instead.
# ---------------------------------------------------------------------------
$interactive = (-not $InstallDir) -and (-not $PSBoundParameters.ContainsKey('InstallDir')) -and (-not $CheckOnly)
if ($interactive) {
    $defaultDir = Join-Path $env:LOCALAPPDATA $Script:AppDirName
    if ($Yes) {
        # v17: non-interactive -- never hang on Read-Host; take the default.
        Write-Host "[install] -Yes: no -InstallDir given -- using default $defaultDir (non-interactive)"
        $InstallDir = $defaultDir
    } else {
        $answer = Read-Host "Install CodeAgent into which directory? (Enter = $defaultDir)"
        if ($answer -and $answer.Trim()) { $InstallDir = $answer.Trim() }
        else { $InstallDir = $defaultDir }
    }
}

# ---------------------------------------------------------------------------
# Phase 0 execution: -CheckOnly runs ONLY the check and exits 0.
# Normal installs run the same check first, print the report, then go on.
# ---------------------------------------------------------------------------
$checkInstallDir = Resolve-InstallDirForCheck $InstallDir
$checkResult = Invoke-EnvironmentCheck $checkInstallDir

if ($CheckOnly) {
    # Strict dry-run: nothing has been written by anything above.
    # (Resolve-InstallDirForCheck resolves WITHOUT creating; every
    # probe in Invoke-EnvironmentCheck is read-only.)
    Write-Host ""
    Write-Host "[check] -CheckOnly: dry-run complete. Nothing was written."
    Write-Host "[check] exit code: 0"
    exit 0
}

# If the check found user-action blockers, surface them BEFORE the
# install does anything destructive (the copy below would overwrite).
# We do NOT hard-fail: the user may have deliberately launched anyway
# (e.g. disk estimate is rough). We make the decision explicit.
if ($checkResult.UserMustFix.Count -gt 0) {
    # v16: -ForceOwnRuntime vs registered same-version is a HARD stop
    # (the official installer cannot lay a second copy -- proceeding
    # would produce a half-usable install directory and exit 0, the
    # exact field failure). No -Force override for THIS one.
    if ($checkResult.RegAction -eq "conflict") {
        Write-Host ""
        throw @"
[install] -ForceOwnRuntime asked for a private Python $PythonVersion copy, but this version is already registered on this machine (sources: $(($checkResult.RegSources) -join '; ')).
The official installer CANNOT install a second copy of the same version (per-version single instance) -- it would silently exit 0.
FIX (pick one):
  (1) re-run WITHOUT -ForceOwnRuntime (the registered interpreter will be reused automatically)
  (2) -UsePbs for a fully private portable copy (no registration involved)
  (3) -PythonVersion 3.12.9 (a patch version not yet taken)
"@
    }
    Write-Host ""
    Write-Warning "[install] Phase 0 found issues you may want to fix first (see report above)."
    $answer = "y"
    if ($Yes) {
        # v17: non-interactive -- default-answer "y" directly (the
        # v16 code only bypassed the prompt under -Force; a -Yes run
        # with closed stdin hung on Read-Host on the field box).
        Write-Host "[install] -Yes: continuing despite Phase 0 warnings (non-interactive)"
    } elseif (-not $Force) {
        $answer = Read-Host "Continue installing anyway? [Y/n]"
    }
    if ($answer -and ($answer -notmatch '^[Yy]')) {
        Write-Host "[install] aborted by user (fix the flagged issues, then re-run)."
        exit 0
    }
}

$InstallDir = Confirm-InstallDir $InstallDir ([bool]$Force)

# ---------------------------------------------------------------------
# v25 (t_27714fcb): resolve the DATA dir EARLY, once, for the whole
# install path. Empty -DataDir (the old default) now means
# <InstallDir>\data -- the app becomes fully self-contained: logs,
# sessions.db, audit.db, Chromium profile ALL inside the one
# movable/deletable tree. An EXPLICIT -DataDir keeps its old meaning
# exactly (user-chosen location, honored via --user-data-dir +
# DESK_AGENT_DATA_DIR in the launcher).
# $Script:DataDirExplicit is consumed by the v22 ownership gate.
# ---------------------------------------------------------------------
$Script:DataDirExplicit = [bool]($DataDir -and $DataDir.Trim())
if (-not $Script:DataDirExplicit) {
    $DataDir = Join-Path $InstallDir "data"
}
$DataDir = [Environment]::ExpandEnvironmentVariables($DataDir)
Write-Host ("[install] data dir: {0} (source: {1})" -f $DataDir, $(if ($Script:DataDirExplicit) { "explicit -DataDir" } else { "default <InstallDir>\data (v25)" }))

# ---------------------------------------------------------------------
# v7: resolve the package dir. Default <InstallDir>\packages; an
# explicit -PackageDir wins. Created only when missing AND creatable;
# a read-only existing dir is FINE (packages are reused from it, new
# ones just cannot be cached there).
# ---------------------------------------------------------------------
if (-not $PackageDir) {
    $PackageDir = Join-Path $InstallDir "packages"
} else {
    $PackageDir = Resolve-PathSafe $PackageDir
}

# v19: resolve -CacheDir the same way (default <InstallDir>\runtime\cache
# only when the switch-argument form was used; "" keeps the default).
if ($CacheDir) {
    $CacheDir = [Environment]::ExpandEnvironmentVariables($CacheDir)
    if (-not [IO.Path]::IsPathRooted($CacheDir)) {
        $CacheDir = Join-Path (Get-Location) $CacheDir
    }
    # Do NOT create it here (env below does, lazily). Normalize the tail
    # so Join-Path below does not double-separate.
    $CacheDir = $CacheDir.TrimEnd('\')
}
$Script:CacheDir = $CacheDir

# v19: self-contained caches. Only when -CacheDir was given: create the
# pip/npm cache subdirs and export env vars so EVERY native child in this
# PowerShell process (python -m pip, node/npm) sees them. GUI installs
# pass <target>\runtime\cache, making the whole tree movable/deletable.
if ($CacheDir) {
    $pipCacheDir = Join-Path $CacheDir "pip"
    $npmCacheDir = Join-Path $CacheDir "npm"
    foreach ($cd in @($pipCacheDir, $npmCacheDir)) {
        if (-not (Test-Path -LiteralPath $cd)) {
            New-Item -ItemType Directory -Path $cd -Force | Out-Null
        }
    }
    $env:PIP_CACHE_DIR = $pipCacheDir
    $env:npm_config_cache = $npmCacheDir
    Write-Host "[install] self-contained package caches (v19 -CacheDir): pip=$pipCacheDir ; npm=$npmCacheDir"
}
if (-not (Test-Path -LiteralPath $PackageDir)) {
    try {
        New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null
        Write-Host "[install] package dir created: $PackageDir"
    } catch {
        Write-Warning "[install] could not create package dir $PackageDir ($($_.Exception.Message)) -- packages will not be cached"
    }
}
$packageDirWritable = Test-DirWritable $PackageDir
if (Test-Path -LiteralPath $PackageDir) {
    if ($packageDirWritable) {
        Write-Host "[install] package dir: $PackageDir (writable -- downloads land here and are reused)"
    } else {
        Write-Host "[install] package dir: $PackageDir (READ-ONLY -- present packages are reused, nothing is written there)"
    }
} else {
    Write-Warning "[install] package dir unavailable: $PackageDir -- every bootstrap will download to a temp dir and NOT be cached"
}
# v7: -WheelDir default is <PackageDir>\wheels (explicit wins). The
# default only ACTIVATES offline pip when that dir already exists
# (auto-detected pre-seeded wheels), otherwise a normal online pip
# install runs -- an empty packages dir must not force offline mode.
$script:DefaultWheelDirHit = $false
if (-not $WheelDir) {
    $candidateWheels = Join-Path $PackageDir "wheels"
    if (Test-Path -LiteralPath $candidateWheels) {
        $WheelDir = $candidateWheels
        $script:DefaultWheelDirHit = $true
        Write-Host "[install] wheels dir auto-detected: $WheelDir (pip will install offline from it)"
    }
}

$sourceRoot = Split-Path -Parent $PSScriptRoot
Write-Host "[install] source:  $sourceRoot"
Write-Host "[install] target:  $InstallDir"

# True offline: -Offline means no network at all. The runtimes must
# come from -PythonArchive/-NodeArchive (or be pre-laid), and pip must
# come from -WheelDir (or the venv must already exist).
$offlinePip = $Offline -or [bool]$WheelDir
if ($Offline -and -not $PythonArchive -and -not (Test-Path -LiteralPath (Join-Path $InstallDir "runtime\python\python.exe"))) {
    Write-Warning "[install] -Offline with no -PythonArchive and no pre-laid python runtime:"
    Write-Warning "          if the machine has no usable Python on PATH, the venv step will fail."
}

# ---------------------------------------------------------------------------
# step 1: copy app tree (skip dev-only / machine-local junk)
# ---------------------------------------------------------------------------
$marker = Join-Path $InstallDir ".install-state.json"
$copyDone = (Test-Path -LiteralPath $marker)
# v9: remember whether the TARGET already owns .env / config.json
# BEFORE any copy -- pre-existing user files are never touched, but
# files the copy writes must be BLANK (Assert-BlankConfig).
$envExistedBefore = Test-Path -LiteralPath (Join-Path $InstallDir ".env")
$cfgExistedBefore = Test-Path -LiteralPath (Join-Path $InstallDir "config.json")
if ($copyDone) {
    Write-Host "[install] app files already copied (marker exists) -- skipping copy"
} else {
    Invoke-Step "copying app files" {
        # NOTE: $args is an automatic variable in PowerShell -- must not assign.
        # /XJ (v4): NEVER traverse junctions/symlinks. Field incident
        # 2026-10-10: ui\node_modules\codeagent is a junction pointing
        # back at the repo root; robocopy followed it and copied the
        # repo into itself (6.6 GB junk). /XD on that dir is the
        # second belt on top of the /XJ suspenders.
        $knownJunctions = @(
            (Join-Path $sourceRoot "ui\node_modules\codeagent"),
            (Join-Path $sourceRoot "node_modules\codeagent")
        )
        # v4: exclude python\.venv too. The OLD code only excluded a
        # ROOT-level .venv; the repo's venv lives at python\.venv, and
        # its home points at the donor machine's interpreter
        # (observed E:\miniconda3 on the .5 box) -- copied venvs are
        # machine-specific garbage here.
        $rcArgs = @($sourceRoot, $InstallDir, "/E", "/XJ", "/R:2", "/W:2", "/NFL", "/NDL", "/NP")
        foreach ($s in @("node_modules", ".git", ".install-state.json", "runtime", "codeagent", "data")) {
            $rcArgs += "/XD"; $rcArgs += (Join-Path $sourceRoot $s)
        }
        # v21 (t_69654390): dev-era junk must never reach a user
        # install. Field finding (field box, 2026-10-13): the install
        # dir ended up with .pytest_cache/, backup-deploy-*/,
        # backup-flow-*/, backup-ui-*/, docs/, tests/, .github/,
        # tsconfig.test.json -- dev tree leftovers (disk waste,
        # internal-structure exposure, test fixtures). /XD is name
        # based; bare names match at any depth. docs/ is dev design
        # notes and research artifacts -> dev-only, excluded.
        # tsconfig.test.json is the only FILE in the set -> /XF below.
        foreach ($xd in @(
            ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
            ".coverage", "htmlcov", ".cache",
            "tests", "test", "__pycache__", ".nyc_output", "coverage",
            "backup-*", "backups"
        )) {
            $rcArgs += "/XD"; $rcArgs += $xd
        }
        foreach ($d in @("docs", ".github")) {
            $srcD = Join-Path $sourceRoot $d
            if (Test-Path -LiteralPath $srcD) { $rcArgs += "/XD"; $rcArgs += $srcD }
        }
        # v21: the scripts subtree carries its own dev harnesses
        # (scripts\tests-v10\*, scripts\git-hooks\*) -- test fixtures
        # embed FAKE sk- keys that the final credential sweep would
        # correctly reject; they must not be copied at all. Excluded
        # by full source path, not by bare name.
        foreach ($s in @("scripts\tests-v10", "scripts\git-hooks")) {
            $srcS = Join-Path $sourceRoot $s
            if (Test-Path -LiteralPath $srcS) { $rcArgs += "/XD"; $rcArgs += $srcS }
        }
        # exclude by NAME anywhere in the tree (robocopy /XD matches
        # full paths, so also add bare names which match any depth):
        $rcArgs += "/XD"; $rcArgs += ".venv"
        foreach ($j in $knownJunctions) {
            if (Test-Path -LiteralPath $j) { $rcArgs += "/XD"; $rcArgs += $j }
        }
        # v9 SECURITY: never copy credentials/state/logs from the
        # source tree. /XF is file-NAME based and applies at every
        # depth, so .env / config.json anywhere in the tree are
        # skipped. (Field incident: a real 240-byte .env and a real
        # 2311-byte config.json rode along to the target.) The HARD
        # backstop below (Assert-BlankConfig) catches anything that
        # still slips through.
        # v21 (t_69654390): the v9 list had a LEAK HOLE -- it covered
        # .env and config.json but NOT their backup siblings. Real
        # field finding (field box, 2026-10-13): config.json.bak-*
        # (dev-period config backups, apiKey/DASHSCOPE field names
        # inside) rode along to a fresh install. Any name that IS a
        # credential or backup of one is excluded by PATTERN:
        foreach ($xf in @(
            ".env", ".env.*", "*.env", "*.env.*",
            "config.json", "config.json.*", "config.json.bak-*",
            "*.bak", "*.bak-*", "*.backup", "*.old", "*.orig",
            "*credentials*", "*secret*", "*.key", "*.pem",
            ".install-state.json", "*.log", "tsconfig.test.json",
            "*.pyc", "*.tsbuildinfo",
            # v25 (t_27714fcb): the data dir now lives INSIDE the
            # install tree -- never let the source repo's dev dbs or
            # the user's own live dbs ride along or get overwritten
            # by a copy. sessions.db is already EXACT-blacklisted in
            # the updater; this is the installer-side mirror.
            "sessions.db", "sessions.db-*", "audit.db", "audit.db-*"
        )) {
            $rcArgs += "/XF"; $rcArgs += $xf
        }
        robocopy @rcArgs
        if ($LASTEXITCODE -ge 8) { throw "[install] robocopy failed (rc=$LASTEXITCODE)" }
        $global:LASTEXITCODE = 0
    }
    # v4: post-copy sanity assert. Expected size = source tree with
    # the SAME exclusions, walked WITHOUT following reparse points.
    # v21: mirror the new /XD dev-junk set so the source expectation
    # counts the same directories robocopy skipped.
    $copyExcludes = @(
        "node_modules", ".git", "runtime", ".install-state.json", "codeagent",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".coverage",
        "htmlcov", ".cache", "tests", "test", "__pycache__",
        ".nyc_output", "coverage", "backups", "docs", ".github", ".venv", "data"
    )
    $srcStats = Get-TreeStats $sourceRoot $copyExcludes
    $dstStats = Get-TreeStats $InstallDir $copyExcludes
    Assert-CopySanity $srcStats $dstStats "app tree copy"
    # v9 SECURITY hard backstop (see Assert-BlankConfig): if the copy
    # wrote a non-blank .env or a non-blank config.json, the install
    # dies HERE -- before any marker is written, before the target is
    # ever shipped.
    Assert-BlankConfig -InstallDir $InstallDir `
        -EnvExistedBefore:$envExistedBefore -CfgExistedBefore:$cfgExistedBefore `
        -ExampleJson (Join-Path $sourceRoot "config.example.json")
    # v21: record the actual top-level layout for the completion
    # report (acceptance: print WHICH top-level dirs were copied).
    $Script:CopiedTopDirs = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name }) | Sort-Object
}

# node_modules: electron itself is needed at runtime -> copy it
# directly from the source machine (pure JS, machine-independent), so
# an offline machine still runs.
$srcNodeModules = Join-Path $sourceRoot "node_modules"
$dstNodeModules = Join-Path $InstallDir "node_modules"
if ((Test-Path -LiteralPath $srcNodeModules) -and -not (Test-Path -LiteralPath $dstNodeModules)) {
    Invoke-Step "copying node_modules (electron runtime, no build needed)" {
        # /XJ (v4): node_modules may itself contain junctions (npm
        # workspace links like ui\node_modules\codeagent); never
        # traverse them (see the 6.6 GB incident).
        robocopy $srcNodeModules $dstNodeModules /E /XJ /R:2 /W:2 /NFL /NDL /NP /MT:8
        if ($LASTEXITCODE -ge 8) { throw "[install] robocopy node_modules failed (rc=$LASTEXITCODE)" }
        $global:LASTEXITCODE = 0
    }
    # v4: post-copy sanity assert for node_modules (junctions under
    # ui\node_modules would balloon the target).
    $nmSrcStats = Get-TreeStats $srcNodeModules @()
    $nmDstStats = Get-TreeStats $dstNodeModules @()
    Assert-CopySanity $nmSrcStats $nmDstStats "node_modules copy"
} elseif (-not (Test-Path -LiteralPath $srcNodeModules)) {
    # v24: the build stage below will 'npm install' into the target, so
    # this is informational only (it used to mean "the app won't run").
    Write-Host "[install] source has no node_modules -- the build stage will run 'npm install' on the target"
}

# ---------------------------------------------------------------------------
# step 2a: ensure a base Python interpreter exists
# ---------------------------------------------------------------------------
$venvPy = Join-Path $InstallDir "python\.venv\Scripts\python.exe"
$venvDir = Join-Path $InstallDir "python\.venv"
$basePy = $null

if ($Offline) {
    # v16: offline resolution order (MUST mirror the online path's
    # bootstrap ability -- this was the v15 bug: offline only did
    # discovery and never touched the local archives, so
    # -PythonArchive / package-dir cache were dead code):
    #   1. <InstallDir>\runtime\python\python.exe   (already laid)
    #   2. <InstallDir>\python\.venv                (already built)
    #   3. -PythonPath (explicit, offline too)
    #   4. discovery (PATH / registry / common locations)
    #   5. -PythonArchive (verified local archive)
    #   6. <PackageDir> cached python installer exe (verified)
    #   7. nothing left -> hard error with the full source list
    $pyDec = Find-Python $PythonPath $InstallDir
    $basePy = $pyDec.Exe
    if (-not $basePy -and $PythonPath) {
        # Find-Python already throws on a missing -PythonPath; this is
        # belt-and-braces for an empty-string edge.
        throw "[install] -PythonPath not found: $PythonPath"
    }
    if ($basePy) {
        Write-Host "[install] python decision: $($pyDec.Reason)"
        Write-Host "[install] -Offline: using existing python at $basePy"
    } else {
        # v18: -PythonArchive now outranks *discovery* on the offline
        # path too (stageE field incident: registry discovery returned
        # a foreign install's python.exe, so the archive was never
        # used and <InstallDir>\runtime\python stayed missing). The
        # explicit archive is the user's intent -- install IT, here.
        # (The laid runtime and -PythonPath were already checked above
        # by Find-Python; only uninvited discovery gets demoted.)
        $archInt = Test-LocalPythonArchiveIntent
        if ($archInt.Intent -and -not (Test-Path -LiteralPath $venvPy)) {
            Write-Host "[install] -Offline: -PythonArchive ($($archInt.Path)) is given -- it takes priority over discovered interpreters; bootstrapping the runtime from the archive"
            $basePy = Install-PythonRuntimeOffline $InstallDir
            if (-not $basePy) {
            throw @"
[install] -Offline: no usable Python found and no venv present. Tried in order:
  (1) <InstallDir>\runtime\python\python.exe -- not present
  (2) <InstallDir>\python\.venv\Scripts\python.exe -- not present
  (3) PATH / registry / common locations -- none runnable
  (4) -PythonArchive -- $(if ($PythonArchive) { "given but failed verification (see warnings above)" } else { "not given" })
  (5) <PackageDir> ($PackageDir) cached $(Get-PackageFileName "PythonInstaller") -- $(if (Test-Path -LiteralPath (Join-Path $PackageDir (Get-PackageFileName "PythonInstaller"))) { "present but failed verification" } else { "not present" })
FIX (pick one):
  - copy python-3.12.10-amd64.exe (or a cpython install_only.tar.gz) to $PackageDir and re-run
  - pass -PythonArchive <path to the exe / tar.gz>
  - lay a Python 3.10+ runtime yourself at <InstallDir>\runtime\python\python.exe
  - install Python on the target machine's PATH
"@
            }
            Write-Host "[install] -Offline: python runtime bootstrapped from local sources: $basePy"
        } elseif (-not (Test-Path -LiteralPath $venvPy)) {
            # v18: no explicit archive and discovery found nothing --
            # v16 behavior preserved (package-dir cache bootstrap or
            # the hard error above).
            Write-Host "[install] python decision: $($pyDec.Reason)"
            $basePy = Install-PythonRuntimeOffline $InstallDir
            if (-not $basePy) {
            throw @"
[install] -Offline: no usable Python found and no venv present. Tried in order:
  (1) <InstallDir>\runtime\python\python.exe -- not present
  (2) <InstallDir>\python\.venv\Scripts\python.exe -- not present
  (3) PATH / registry / common locations -- none runnable
  (4) -PythonArchive -- $(if ($PythonArchive) { "given but failed verification (see warnings above)" } else { "not given" })
  (5) <PackageDir> ($PackageDir) cached $(Get-PackageFileName "PythonInstaller") -- $(if (Test-Path -LiteralPath (Join-Path $PackageDir (Get-PackageFileName "PythonInstaller"))) { "present but failed verification" } else { "not present" })
FIX (pick one):
  - copy python-3.12.10-amd64.exe (or a cpython install_only.tar.gz) to $PackageDir and re-run
  - pass -PythonArchive <path to the exe / tar.gz>
  - lay a Python 3.10+ runtime yourself at <InstallDir>\runtime\python\python.exe
  - install Python on the target machine's PATH
"@
            }
            Write-Host "[install] -Offline: python runtime bootstrapped from local sources: $basePy"
        }
        # (no archive intent and a venv already exists: fall through,
        # the venv-reuse branch below handles it)
    }
} elseif (Test-Path -LiteralPath $venvPy) {
    Write-Host "[install] venv already exists at $venvPy -- reusing (skip venv build)"
} else {
    $pyDec = Find-Python $PythonPath $InstallDir
    $basePy = $pyDec.Exe
    Write-Host "[install] python decision: $($pyDec.Reason)"
    if (-not $basePy) {
        Write-Host "[install] no usable Python found -- auto-bootstrapping a private runtime (3.12 line)"
        $basePy = Install-PythonRuntime $InstallDir
    } else {
        # v18: an interpreter OUTSIDE <InstallDir> is a live external
        # dependency (stageE field incident: nobody noticed the venv
        # pointed at a neighboring install until that directory was
        # deleted days later). Say so explicitly, every time.
        if (([string]$basePy) -notlike (([string]$InstallDir).TrimEnd('\') + '\*')) {
            # v18.1: wording must match actual behavior (task
            # t_729065d5, real-machine the field box feedback):
            #   - -PythonArchive alone can be overridden by a
            #     registered same-version REUSE; it only wins with
            #     -Offline (v18 priority fix).
            #   - -ForceOwnRuntime is a HARD error when the same
            #     version is already registered (WiX single
            #     instance) -- it is not a "get self-contained
            #     runtime" escape hatch here.
            Write-Warning "[install] reusing an EXTERNAL interpreter -- the venv will depend on it: $basePy (if that directory is ever deleted, this install's venv breaks). For a self-contained runtime: pass -Offline -PythonArchive <archive> (the explicit archive beats registry reuse only with -Offline), or use -UsePbs (portable, no registry); note -ForceOwnRuntime hard-errors if Python $PythonVersion is already registered on this machine."
        }
        Write-Host "[install] using Python at $basePy (no download needed)"
    }
}

# ---------------------------------------------------------------------------
# step 2b: venv
# ---------------------------------------------------------------------------
if ($basePy -and (Test-Path -LiteralPath $basePy) -and -not (Test-Path -LiteralPath $venvPy)) {
    Invoke-Step "creating venv" {
        & $basePy -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "[install] venv creation failed" }
    }
    # v6: pin the venv's pip to the Tsinghua index forever -- any
    # later manual "pip install" inside this venv (by the user, by
    # a future installer run) hits the mirror, not pypi.org.
    # pip.ini is the Windows location pip honors inside a venv.
    $pipIniDir = Join-Path $venvDir ""   # (unused; kept for clarity)
    $pipIni = Join-Path $venvDir "pip.ini"
    if (-not (Test-Path -LiteralPath $pipIni)) {
        $pipIniContent = @"
[global]
index-url = $IndexUrl
trusted-host = $(($IndexUrl -replace '^https?://','' -replace '/.*$',''))
"@
        # v19: when -CacheDir was given, ALSO pin the venv's pip cache
        # inside the self-contained tree, so the user's later manual
        # "pip install" in this venv keeps caching locally (complement
        # of $env:PIP_CACHE_DIR, which covers only installer-time runs).
        if ($Script:CacheDir) {
            # v21: PowerShell drops the line break right before the closing
            # "@ of a here-string, so $pipIniContent ends WITHOUT a newline.
            # The append therefore has to start with CRLF -- otherwise the
            # file ends up with "trusted-host = <host>cache-dir = D:\...\pip"
            # on ONE line, pip splits that value on whitespace, treats the
            # path as a second trusted host and dies with
            # "ValueError: Port could not be cast to integer value as
            #  '\...\runtime\cache\pip'" -- which killed BOTH the mirror and
            # the PyPI fallback and aborted the whole install (the field box real-machine
            # repro 2026-10-13 02:0x / 08:0x).
            $pipIniContent += "`r`n" + "cache-dir = " + (Join-Path $Script:CacheDir "pip") + "`r`n"
        }
        [IO.File]::WriteAllText($pipIni, $pipIniContent, (New-Object Text.UTF8Encoding($false)))
        Write-Host "[install] venv pip.ini written (index pinned to $IndexUrl$(if ($Script:CacheDir) { "; cache pinned to $(Join-Path $Script:CacheDir 'pip')" }))"
        # read-back self-check
        $readBack = Get-Content -LiteralPath $pipIni -Raw
        if ($readBack -notmatch [regex]::Escape($IndexUrl)) {
            throw "[install] pip.ini self-check failed: index-url not written verbatim ($pipIni)"
        }
        # v21: the v19 -CacheDir append glued cache-dir onto the trusted-host
        # line (see above). Assert BOTH keys are on their own line, so a
        # regression can never again ship a pip.ini that pip cannot parse.
        $readLines = @($readBack -split "`r?`n")
        $thLine = @($readLines | Where-Object { $_ -match '^\s*trusted-host\s*=' })
        $cdLine = @($readLines | Where-Object { $_ -match '^\s*cache-dir\s*=' })
        if ($thLine.Count -ne 1) {
            throw "[install] pip.ini self-check failed: trusted-host must occupy exactly one line (got $($thLine.Count)) in $pipIni"
        }
        if ($thLine[0] -match '\\' -or $thLine[0] -match '\s\S+=') {
            throw "[install] pip.ini self-check failed: trusted-host line is polluted ('$($thLine[0])') -- cache-dir must not be glued onto it ($pipIni)"
        }
        if ($Script:CacheDir) {
            if ($cdLine.Count -ne 1) {
                throw "[install] pip.ini self-check failed: -CacheDir was given but cache-dir does not occupy its own line (got $($cdLine.Count)) in $pipIni"
            }
            if ($cdLine[0] -notmatch [regex]::Escape($Script:CacheDir)) {
                throw "[install] pip.ini self-check failed: cache-dir line does not point at -CacheDir ('$($cdLine[0])') in $pipIni"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# step 2c: requirements (wheel-dir aware, fail-soft guard file)
# ---------------------------------------------------------------------------
$reqs = @("python\requirements.txt", "python\requirements-docs.txt")
$optionalReq = "python\requirements-guard.txt"
if (-not $offlinePip) {
    $venvPy = Join-Path $InstallDir "python\.venv\Scripts\python.exe"
    & $venvPy -m pip install --upgrade pip --quiet
    foreach ($req in $reqs) {
        $path = Join-Path $InstallDir $req
        if (-not (Test-Path -LiteralPath $path)) { throw "[install] missing requirements file: $path" }
        Invoke-Step "installing $req (index: $IndexUrl, fallback: official PyPI)" {
            & $venvPy -m pip install -i $IndexUrl -r $path
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "[install] mirror failed, falling back to official PyPI for $req"
                & $venvPy -m pip install -r $path
                if ($LASTEXITCODE -ne 0) { throw "[install] pip failed on $req (both mirror and PyPI)" }
            }
        }
    }
} elseif ($WheelDir) {
    if (-not (Test-Path -LiteralPath $WheelDir)) { throw "[install] -WheelDir not found: $WheelDir" }
    $venvPy = Join-Path $InstallDir "python\.venv\Scripts\python.exe"
    foreach ($req in $reqs) {
        $path = Join-Path $InstallDir $req
        if (-not (Test-Path -LiteralPath $path)) { throw "[install] missing requirements file: $path" }
        Invoke-Step "installing $req offline from $WheelDir" {
            & $venvPy -m pip install --no-index --find-links $WheelDir -r $path
            if ($LASTEXITCODE -ne 0) { throw "[install] offline pip failed on $req (wheel dir incomplete? use 'pip download -r ...' to fill it)" }
        }
    }
} else {
    # plain -Offline without -WheelDir: venv must already be complete
    if (Test-Path -LiteralPath $venvPy) {
        Write-Host "[install] -Offline (no -WheelDir): skipping pip entirely, trusting the existing venv"
    } else {
        Write-Warning "[install] -Offline with no venv and no -WheelDir: sidecar deps are NOT installed"
    }
}

$guardPath = Join-Path $InstallDir $optionalReq
if (-not $offlinePip) {
    if (-not (Test-Path -LiteralPath $guardPath)) {
        Write-Warning "[install] optional $optionalReq missing (skipped)"
    } else {
        & $venvPy -m pip install -i $IndexUrl -r $guardPath
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "[install] requirements-guard.txt failed (optional; guard degrades to regex-only)"
        }
    }
}

# self-check: the 5 packages bootstrap checks
if (Test-Path -LiteralPath $venvPy) {
    Invoke-Step "self-check (Pillow/pymupdf/pypdf/openpyxl/python-docx)" {
        & $venvPy -c @'
import sys
print('  python', sys.version.split()[0])
for mod, label in (('PIL','Pillow'),('fitz','pymupdf'),('pypdf','pypdf'),
                   ('openpyxl','openpyxl'),('docx','python-docx')):
    try:
        __import__(mod); print('  OK   ', label)
    except Exception as exc:
        print('  MISS ', label, '->', type(exc).__name__)
'@
    }
}

# ---------------------------------------------------------------------------
# step 2d: node runtime (OPTIONAL, fail-soft)
# The app runs on the bundled electron.exe and does NOT need Node at
# runtime. Node serves `npm start` fallback, UI rebuilds, and tests.
# ---------------------------------------------------------------------------
$nodeExe = $null
if (-not $NoNode) {
    $nodeExe = Join-Path $InstallDir "runtime\node\node.exe"
    if (Test-Path -LiteralPath $nodeExe) {
        Write-Host "[install] node runtime present: $nodeExe"
    } elseif ($Offline -and -not $NodeArchive) {
        Write-Host "[install] -Offline without -NodeArchive: skipping node bootstrap"
    } else {
        try {
            Install-NodeRuntime $InstallDir | Out-Null
        } catch {
            Write-Warning "[install] node bootstrap FAILED (non-fatal -- the app itself does not need Node):"
            Write-Warning "          $($_.Exception.Message)"
            Write-Warning "          Node only serves 'npm start' fallback / UI rebuilds / tests. Install it later from https://nodejs.org if needed."
            $nodeExe = $null
        }
    }
    # v19: -CacheDir bookkeeping for npm -- the .bat already pins the
    # PATH; ALSO pin npm's cache by a runtime-local .npmrc next to the
    # launcher so later manual `npm` usage inside the install dir keeps
    # caching inside the self-contained tree (install-time npm runs were
    # covered by $env:npm_config_cache set above).
    if ($Script:CacheDir -and (Test-Path -LiteralPath $nodeExe)) {
        $npmRc = Join-Path $InstallDir "runtime\cache\.npmrc"
        try {
            New-Item -ItemType Directory -Path (Split-Path -Parent $npmRc) -Force | Out-Null
            [IO.File]::WriteAllText($npmRc, "cache=" + (Join-Path $Script:CacheDir "npm") + "`r`n", (New-Object Text.UTF8Encoding($false)))
            Write-Host "[install] npm cache pinned via $npmRc"
        } catch {
            Write-Warning "[install] could not write $npmRc ($($_.Exception.Message)) -- manual npm runs will use the global cache"
        }
    }
}

# ---------------------------------------------------------------------------
# step 2d+: BUILD (v24, t_ab909828). The app's launch-critical files are
# gitignored build products, not repo content; the old installer only
# WARNED "target machine will need 'npm install' before first launch"
# and finished green -- the user then got a BLACK WINDOW (loadFile of a
# nonexistent electron\ui\index.html). Online installs now BUILD the
# three artifacts themselves, with the installer's own Node. A failed
# build is a HARD failure (with the manual remedy). -SkipBuild and
# -Offline skip the build but say plainly that the installed app will
# NOT start as-is.
# ---------------------------------------------------------------------------
$Script:BuiltOk = $false
if ($SkipBuild -or $Offline) {
    $why = if ($SkipBuild) { "-SkipBuild" } else { "-Offline" }
    # NOTE: "${why}:" not "$why:" -- inside a double-quoted string a bare
    # $why: is parsed as a SCOPED variable reference and PS 5.1 dies.
    Write-Warning "[install] ${why}: skipping the app build (dist\ + electron\ui\ will NOT be produced)."
    Write-Warning "[install] ${why}: the installed app WILL NOT START as-is (it opens to a black window) until you build it:"
    Write-Warning "[install] ${why}:   cd $InstallDir"
    Write-Warning "[install] ${why}:   npm install (needs Node; npmmirror registry/ELECTRON_MIRROR recommended)"
    Write-Warning "[install] ${why}:   npm run build"
    Write-Warning "[install] ${why}:   powershell -ExecutionPolicy Bypass -File scripts\build-ui.ps1"
} elseif ($nodeExe -and (Test-Path -LiteralPath $nodeExe)) {
    Invoke-AppBuild $InstallDir $nodeExe
    $Script:BuiltOk = $true
} else {
    # node bootstrap failed / -NoNode: still TRY the build with any
    # system npm; if there is none, this throws with the FIX list.
    Invoke-AppBuild $InstallDir $null
    $Script:BuiltOk = $true
}

# v24 (t_ab909828): HARD final gate. The launcher (CodeAgent.bat) runs
# electron.exe with dist\electron\main.js and the main process loads
# electron\ui\index.html. Any of the three missing -> the app is dead
# (black window / npm start fallback that also fails). The install
# must FAIL, not warn -- the field incident was exactly a "green"
# install of a non-starting app.
$artifacts = @(
    @{ Path = Join-Path $InstallDir "dist\electron\main.js";  What = "compiled Electron main (npm run build)" },
    @{ Path = Join-Path $InstallDir "electron\ui\index.html"; What = "staged UI (scripts\build-ui.ps1)" },
    @{ Path = Join-Path $InstallDir "node_modules\electron\dist\electron.exe"; What = "Electron runtime binary" }
)
$missing = @($artifacts | Where-Object { -not (Test-Path -LiteralPath $_.Path) })
if ($missing.Count -gt 0) {
    $lines = ($missing | ForEach-Object { "  - $($_.Path)  ($($_.What))" }) -join "`n"
    throw @"
[install] ARTIFACT CHECK FAILED -- install not completed. Missing launch-critical file(s):
$lines
The app would open to a BLACK WINDOW with these absent. FIX (run inside $InstallDir, needs Node):
  npm install
  npm run build
  powershell -ExecutionPolicy Bypass -File scripts\build-ui.ps1
then re-run this installer (idempotent) to refresh the launcher/shortcuts.
"@
}
foreach ($a in $artifacts) { Write-Host "[install] artifact OK: $($a.Path)" }

# ---------------------------------------------------------------------------
# step 3: launcher CodeAgent.bat (%~dp0-relative, no drive letters)
# ---------------------------------------------------------------------------
$bat = Join-Path $InstallDir "CodeAgent.bat"
Invoke-Step "writing launcher $bat" {
    # Prefer the bundled electron.exe directly: no Node needed at
    # launch. The compiled main (dist\electron\main.js) and the
    # staged UI (electron\ui\index.html) are BUILD PRODUCTS -- the
    # repo gitignores BOTH (lines 2 and 26), so the v24 installer
    # BUILDS them itself (Invoke-AppBuild) and hard-fails if any
    # is missing (the artifact gate above). The old claim that
    # main.js being repo content was FALSE (field incident
    # 2026-10-13: black window on a fresh clone install).
    # If a private node runtime was installed, prepend its dir to PATH
    # for this process only, so the npm fallback and any future `npm`
    # usage work without a system Node. (Field-tested in v1 WITHOUT
    # this block; the block itself is new in v2 -- NOT field-tested.)
    # v25 (t_27714fcb): TWO data switches, split per the task comment:
    #   --user-data-dir  -> Chromium profile dir (Cache/Network/
    #                      Preferences). Explicit -DataDir -> exactly
    #                      that dir (v1..v24 semantics). Default ->
    #                      <InstallDir>\userdata (NOT \data: avoids the
    #                      data\data double-nesting -- Electron would
    #                      otherwise put the sidecar data under
    #                      <userData>\data).
    #   DESK_AGENT_DATA_DIR -> sidecar data (logs/sessions.db/audit.db
    #                      /memory/runs/attachments) in the
    #                      RESOLVED data dir. electron/main.ts (v24)
    #                      now respects an inherited value; only when
    #                      absent does it fall back to <userData>\data
    #                      (legacy %APPDATA% installs keep working).
    # Both are %~dp0-relative when the data dir is the in-tree default
    # -- so moving the whole install directory needs no launcher edit.
    $resolvedData = [Environment]::ExpandEnvironmentVariables($DataDir)
    $userProfileArg = ' --user-data-dir="%~dp0userdata"'
    if ($Script:DataDirExplicit) {
        $userProfileArg = ' --user-data-dir="' + $resolvedData + '"'
    }
    # The env var is a single set "..." line, shared by both branches.
    $envDataLine = 'set "DESK_AGENT_DATA_DIR=' + $resolvedData + '"'
    # NOTE: PowerShell 5.1 array literals can split a `'str' + $var`
    # element into TWO elements. Observed on a real Windows box: the
    # switch landed on its own line in the .bat, which silently broke
    # the launch (electron got no --user-data-dir and died on the
    # single-instance lock). Build the complete lines first instead.
    $pathSetup = ""
    if ($nodeExe -and (Test-Path -LiteralPath $nodeExe)) {
        $pathSetup = 'set "PATH=%~dp0runtime\node;%PATH%"'
    }
    $electronLine = '  "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."' + $userProfileArg
    $npmLine = '  call npm start --' + $userProfileArg
    $lines = @(
        "@echo off",
        'cd /d "%~dp0"',
        $pathSetup,
        $envDataLine,
        'if exist "%~dp0node_modules\electron\dist\electron.exe" (',
        $electronLine,
        ') else (',
        $npmLine,
        ')'
    )
    # batch files want CRLF + ASCII; WriteAllLines default is fine for ASCII content
    [IO.File]::WriteAllLines($bat, $lines, (New-Object Text.UTF8Encoding($false)))
    # Read back: one command per line, and the data dir switch (if any)
    # must sit on the same line as the command it belongs to.
    $written = @(Get-Content -LiteralPath $bat)
    if ($written -notcontains $electronLine) {
        throw "launcher self-check failed: electron line not written verbatim ($bat)"
    }
    if ($written -notcontains $envDataLine) {
        throw "launcher self-check failed: DESK_AGENT_DATA_DIR line not written verbatim ($bat)"
    }
    foreach ($l in $written) {
        if ($l -match '--user-data-dir' -and $l -notmatch 'electron\.exe|npm start') {
            throw "launcher self-check failed: --user-data-dir ended up on its own line ($bat)"
        }
    }
}

# config.json: copy blank template if not present (never overwrite
# existing -- a user-filled config is the user's). v9: the template is
# the ONLY source; the real source-tree config.json never arrives (see
# the /XF exclusion + Assert-BlankConfig above).
$cfgDst = Join-Path $InstallDir "config.json"
$cfgSrc = Join-Path $sourceRoot "config.example.json"
if (-not (Test-Path -LiteralPath $cfgDst) -and (Test-Path -LiteralPath $cfgSrc)) { Copy-Item -LiteralPath $cfgSrc -Destination $cfgDst }
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir ".env"))) {
    [IO.File]::WriteAllText((Join-Path $InstallDir ".env"), "",
        (New-Object Text.UTF8Encoding($false)))  # UTF-8 no BOM, per README
}
# v9: what the completion report says about config state. BLANK = no
# usable model (model.baseUrl / model.model empty) or no key-ish
# non-empty field. User-owned pre-existing configs are reported as
# their own.
$cfgIsBlank = $true
if (Test-Path -LiteralPath $cfgDst) {
    try {
        $cfgObj = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
        $m = $cfgObj.model
        if ($m -and (([string]$m.baseUrl).Trim() -ne "" -or ([string]$m.model).Trim() -ne "")) { $cfgIsBlank = $false }
    } catch { $cfgIsBlank = $false }
}

# v26 (t_2d0783e3): default working directory. Resolve: explicit
# -WorkspaceDir > <InstallDir>\workspace. Create it (plus a README
# explaining what it is), then write the absolute path into
# config.json's capabilities.workspaceRoot. Non-overwrite rule: an
# existing non-empty capabilities.workspaceRoot survives UNLESS this
# run passed -WorkspaceDir explicitly (explicit intent wins). Atomic
# write via temp file + rename so a crash never leaves half a config.
$workspaceDirDefault = Join-Path $InstallDir "workspace"
$workspaceDir = if ($WorkspaceDir -and $WorkspaceDir.Trim()) { $WorkspaceDir.Trim() } else { $workspaceDirDefault }
$workspaceDir = [Environment]::ExpandEnvironmentVariables($workspaceDir)
$workspaceDir = [IO.Path]::GetFullPath($workspaceDir)   # absolute, rooted at the process cwd if drive-relative
$Script:WorkspaceDirResolved = $workspaceDir
$workspaceReadme = Join-Path $workspaceDir "README.txt"
$workspaceReadmeText = @"
This is the default working directory (workspace) for CodeAgent.

Files the agent reads/writes with relative paths land here by default
(read_file, write_file, run_shell cwd, ...). The setting lives in
config.json -> capabilities.workspaceRoot and can be changed by asking
the agent in a conversation ("switch the workspace to D:\whatever")
or by editing config.json directly.
"@
if (-not (Test-Path -LiteralPath $workspaceDir)) {
    New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $workspaceReadme)) {
    [IO.File]::WriteAllText($workspaceReadme, $workspaceReadmeText,
        (New-Object Text.UTF8Encoding($false)))   # UTF-8 no BOM
}
# --- config.json: capabilities.workspaceRoot ---
$workspaceWroteConfig = $false
if (Test-Path -LiteralPath $cfgDst) {
    $cfgWs = $null
    try { $cfgWs = (Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json).capabilities.workspaceRoot } catch { $cfgWs = $null }
    $existingWs = if ($cfgWs) { [string]$cfgWs } else { "" }
    if ((-not $existingWs.Trim()) -or $WorkspaceDir) {
        # blank/absent (fill with the default) or explicit -WorkspaceDir
        # (explicit user intent wins over the stored value)
        try {
            $cfgWsObj = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
            if (-not $cfgWsObj.capabilities) {
                # PS 5.1: adding a property to $null only works via
                # Add-Member on an existing object; create a fresh one.
                $cfgWsObj | Add-Member -NotePropertyName capabilities -NotePropertyValue ([pscustomobject]@{}) -ErrorAction Stop
            }
            $capObj = $cfgWsObj.capabilities
            if ($capObj.PSObject.Properties["workspaceRoot"]) {
                $capObj.workspaceRoot = $workspaceDir
            } else {
                $capObj | Add-Member -NotePropertyName workspaceRoot -NotePropertyValue $workspaceDir -ErrorAction Stop
            }
            $wsJson = $cfgWsObj | ConvertTo-Json -Depth 32
            $wsTmp = "$cfgDst.ws.tmp"
            [IO.File]::WriteAllText($wsTmp, $wsJson, (New-Object Text.UTF8Encoding($false)))
            # Atomic swap: [IO.File]::Replace(tmp, dst, $null) rejects an empty
            # backup-path string on .NET (field-tested), so use Move-Item -Force
            # (atomic rename on the same volume; the temp file sits next to the
            # target, so it IS the same volume).
            Move-Item -LiteralPath $wsTmp -Destination $cfgDst -Force
            $workspaceWroteConfig = $true
        } catch {
            Write-Warning "[install] could not write capabilities.workspaceRoot to config.json: $_ (the workspace dir itself was created)"
        }
    }
}

# v21 (t_69654390): FINAL whole-tree credential sweep, after every
# copy (app tree, node_modules, runtime, templates) has landed. This
# is the acceptance line: "no sk- plaintext or valued apiKey anywhere
# under the finished install tree". A user's own filled-in .env /
# config.json (pre-existing BEFORE the copy, or just created from the
# blank template for them to fill) is THEIRS, not a leak -- excluded
# via -SkipFiles. Everything else must be clean.
$finalSkip = @()
foreach ($p in @((Join-Path $InstallDir ".env"), $cfgDst)) {
    if ($p -and (Test-Path -LiteralPath $p)) { $finalSkip += $p }
}
Invoke-CredentialSweep -Root $InstallDir -SkipFiles $finalSkip

# completion marker (idempotency)
# v17: record the RESOLVED data dir so a later -Uninstall -PurgeData
# resolves the same target the install actually used (explicit
# -DataDir > this record > Electron default -- one rule set, both
# sides).
# v22 (t_a8ceb810): the ownership marker is now written through the
# ownership PROOF (Write-DataOwnershipMarkerIfOwned), never
# unconditionally: installing to D:\ elsewhere must not stamp the
# user's real data dir as "belonging to this install" -- that stamp
# was exactly the -PurgeData mis-delete setup. Marker only when this
# install owns the dir (explicit -DataDir / inside InstallDir / same
# prior stamp); otherwise read-only + warning. The uninstall-side
# record still notes the resolved dataDir so -PurgeData can NAME the
# dir, but purge still requires the ownership proof above.
# v25 note: $Script:DataDirExplicit was resolved EARLY (right after
# Confirm-InstallDir) -- since v25 $DataDir is never empty on the
# install path, so the late recomputation below would always read
# "explicit". Keep the variable, but use the EARLY value: the v22
# ownership gate must distinguish real user intent from the new
# in-tree default. With the default, ownership is proven by the
# "inside the InstallDir" rule instead.
$Script:ResolvedDataDir = (Resolve-DataDir $DataDir $InstallDir).DataDir
Write-DataOwnershipMarkerIfOwned $Script:ResolvedDataDir $InstallDir
@{ installed = $true; installedAt = (Get-Date).ToString("s"); installDir = $InstallDir; dataDir = $Script:ResolvedDataDir } |
    ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8

# v25 (t_27714fcb): legacy-user compatibility. If this install uses
# the new in-tree default (<InstallDir>\data, does not exist yet)
# while an OLD Electron-default dir (%APPDATA%\CodeAgent) exists and
# is non-empty, we do NOT auto-move and do NOT delete anything -- the
# task rule. One line in the final report points the user at it.
$Script:LegacyDataHint = ""
if (-not $Script:DataDirExplicit) {
    $legacyDir = Join-Path $env:APPDATA $Script:AppDirName
    if ((Test-PathSafe $legacyDir) -and -not (Test-PathSafe (Join-Path $InstallDir "data"))) {
        $hasContent = $false
        try { $hasContent = [bool](Get-ChildItem -LiteralPath $legacyDir -Force -ErrorAction SilentlyContinue | Select-Object -First 1) } catch { }
        if ($hasContent) {
            $Script:LegacyDataHint = "legacy"
        }
    }
}

# ---------------------------------------------------------------------------
# step 4: shortcuts
# ---------------------------------------------------------------------------
if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $targets = @(
        @{ Ln = Join-Path ([Environment]::GetFolderPath('Desktop')) "$($Script:AppDirName).lnk" },
        @{ Ln = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$($Script:AppDirName).lnk" }
    )
    foreach ($t in $targets) {
        if (Test-Path -LiteralPath $t.Ln) {
            Write-Host "[install] shortcut exists, skipped: $($t.Ln)"
        } else {
            $sc = $shell.CreateShortcut($t.Ln)
            $sc.TargetPath = $bat
            $sc.WorkingDirectory = $InstallDir
            $sc.Description = "CodeAgent"
            $sc.Save()
            Write-Host "[install] shortcut created: $($t.Ln)"
        }
    }
}

# ---------------------------------------------------------------------------
# final report
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "==================== install complete ===================="
Write-Host "  app dir:     $InstallDir"
# v26 (t_2d0783e3): the default working directory is part of the report
# (requirement: print the ACTUAL resolved path). Also state whether the
# config.json write happened, so "workspace exists but config didn't
# land" is visible instead of silent.
Write-Host "  workspace:   $Script:WorkspaceDirResolved$(if ($workspaceWroteConfig) { " (written to config.json -> capabilities.workspaceRoot)" } else { " (config.json left as-is -- existing workspaceRoot kept)" })"
Write-Host "  data dir:    $Script:ResolvedDataDir$(if ($Script:DataDirExplicit) { " (explicit -DataDir; via --user-data-dir + DESK_AGENT_DATA_DIR)" } else { " (default: <InstallDir>\data -- sidecar logs/sessions.db; Chromium profile in <InstallDir>\userdata)" })"
if ($Script:LegacyDataHint -eq "legacy") {
    $legacyDir = Join-Path $env:APPDATA $Script:AppDirName
    Write-Host "  note:        an old user data directory exists at $legacyDir -- this install does NOT use it; move it manually to $(Join-Path $InstallDir "data") if you want to keep those sessions"
}
if ($nodeExe -and (Test-Path -LiteralPath $nodeExe)) {
    Write-Host "  node runtime: $nodeExe (for npm start fallback / rebuilds / tests)"
} elseif (-not $NoNode) {
    Write-Host "  node runtime: NOT installed (app does not need it; see warnings above)"
}
if ($Script:CacheDir) {
    Write-Host "  package caches: $Script:CacheDir (pip\, npm\ -- inside the install tree; deleting/moving the whole directory leaves no machine residue)"
}
# v24 (t_ab909828): the build state and the UI artifact path are part of
# the completion report (field incident: "green" install, black window,
# nobody could tell whether anything had been built).
if ($Script:BuiltOk) {
    Write-Host "  built:       YES -- dist\electron\main.js + electron\renderer.bundle.js + electron\ui\index.html"
    Write-Host "  UI:          $InstallDir\electron\ui\index.html"
} elseif ($SkipBuild -or $Offline) {
    Write-Host "  built:       NO (-SkipBuild/-Offline) -- the app WILL NOT START until you build it (see warnings above)"
    Write-Host "  UI:          (absent -- build with scripts\build-ui.ps1)"
} else {
    Write-Host "  built:       ??? (internal error -- BuildOk unset without SkipBuild/Offline; report this)"
}
Write-Host "  launch:      double-click $bat or the Start Menu / Desktop shortcut"
if ($cfgIsBlank) {
    # v9: always state the blank-config fact on a fresh install -- the
    # field incident was exactly that nobody noticed the real config
    # had been copied. If this line EVER prints with a non-empty
    # config, the assert above failed to run.
    Write-Host "  config:      BLANK -- edit $InstallDir\config.json (models) and $InstallDir\.env (keys) before use"
} else {
    Write-Host "  config:      present (user-filled config.json detected)"
}
# v21 (t_69654390): show the user exactly what landed in the install
# dir (acceptance: print "which top-level directories were actually
# copied"). With the v21 /XD list, no dev-era residue should appear.
$dirList = $Script:CopiedTopDirs
if (-not $dirList -or $dirList.Count -eq 0) {
    $dirList = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name }) | Sort-Object
}
if ($dirList.Count -gt 0) {
    Write-Host "  copied:      top-level: $($dirList -join ', ')"
}
$devResidue = @($dirList | Where-Object { $_ -match '^(tests?|\.pytest_cache|\.github|docs|\.mypy_cache|\.ruff_cache|\.coverage|htmlcov|coverage|__pycache__|backup-.*|backups|\.cache|\.nyc_output|\.tox)$' -or $_ -eq 'tsconfig.test.json' })
if ($devResidue.Count -gt 0) {
    Write-Warning "[install] dev-era residue still present at top level: $($devResidue -join ', ') -- check the /XD exclusion list"
}
Write-Host "==========================================================="

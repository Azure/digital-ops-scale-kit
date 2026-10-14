"""Describe PowerShell scripts with the native Windows PowerShell parser.

Workflow tests use this to assert structure, such as which command receives a
variable, rather than matching rendered text. Windows PowerShell 5.1 is the
parser the hosted Windows steps run, so callers skip on other platforms.
"""

import base64
import json
import os
import subprocess

_DESCRIBE = r"""
$ErrorActionPreference = 'Stop'
$source = [Console]::In.ReadToEnd()
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
$language = 'System.Management.Automation.Language'
function Find([string]$Name) {
    $type = "$language.$Name" -as [type]
    @($ast.FindAll({ param($node) $node -is $type }.GetNewClosure(), $true))
}
function Enclosing($Node, [string]$Name) {
    $type = "$language.$Name" -as [type]
    for ($parent = $Node.Parent; $parent; $parent = $parent.Parent) {
        if ($parent -is $type) { return $parent }
    }
    return $null
}
$commands = foreach ($node in Find 'CommandAst') {
    [ordered]@{
        name = [string]$node.GetCommandName()
        start = $node.Extent.StartOffset
        parameters = @($node.CommandElements | Where-Object {
            $_ -is [System.Management.Automation.Language.CommandParameterAst]
        } | ForEach-Object { $_.ParameterName })
        text = $node.Extent.Text
    }
}
$variables = foreach ($node in Find 'VariableExpressionAst') {
    $command = Enclosing $node 'CommandAst'
    $assignment = Enclosing $node 'AssignmentStatementAst'
    [ordered]@{
        name = $node.VariablePath.UserPath
        start = $node.Extent.StartOffset
        parent = $node.Parent.GetType().Name
        parentText = $node.Parent.Extent.Text
        command = if ($command) { [string]$command.GetCommandName() } else { '' }
        assigned = [bool]($assignment -and $assignment.Left.Extent.StartOffset -eq $node.Extent.StartOffset)
        assignmentTarget = if ($assignment) { $assignment.Left.Extent.Text } else { '' }
    }
}
$functions = foreach ($node in Find 'FunctionDefinitionAst') {
    [ordered]@{ name = $node.Name; text = $node.Extent.Text }
}
$hereStrings = foreach ($node in Find 'AssignmentStatementAst') {
    $value = $node.Right.Expression
    if ($value -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $value.StringConstantType -eq 'SingleQuotedHereString') {
        [ordered]@{ target = $node.Left.Extent.Text; value = $value.Value }
    }
}
$finallyBlocks = foreach ($node in Find 'TryStatementAst') {
    if ($node.Finally) { $node.Finally.Extent.Text }
}
[Console]::Out.Write((ConvertTo-Json -Depth 6 -Compress ([ordered]@{
    errors = @($errors | ForEach-Object { $_.Message })
    commands = @($commands)
    variables = @($variables)
    functions = @($functions)
    hereStrings = @($hereStrings)
    finallyBlocks = @($finallyBlocks)
})))
"""


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def describe(script: str) -> dict:
    """Return parse errors, commands, variable uses, functions, here-strings and finally blocks."""
    # Bytes keep line endings exact, so a backtick continuation is never followed by a doubled CR.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(_DESCRIBE)],
        input=script.replace("\r\n", "\n").encode("utf-8"), capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return json.loads(result.stdout.decode("utf-8"))


def function_source(description: dict, name: str) -> str:
    """Return the exact source of one function the script defines."""
    matches = [item["text"] for item in description["functions"] if item["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one function named {name}.")
    return matches[0]


def run(script: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a script body in a fresh Windows PowerShell 5.1 process without a profile.

    A PowerShell 7 parent's module path would hide Windows PowerShell's own
    modules, so the child computes its default path as a workflow step does.
    """
    environment = {name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"}
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(script)],
        text=True, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        env=environment,
    )

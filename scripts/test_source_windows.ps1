[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# These integration modules intentionally require excluded local artifacts or
# sibling addon/simulator trees. They remain covered by test_windows.ps1.
$excluded = @(
    'test_addon_calibration_contract.py',
    'test_cat2_zero_config_bridge.py',
    'test_fury_chronicle_prior.py',
    'test_fury_combined_shadow_policy.py',
    'test_fury_current_build_phase12.py',
    'test_fury_current_build_phase12_review.py',
    'test_fury_current_cat2_heldout_replay_v1.py',
    'test_fury_current_cat2_noop_adjudication_v1.py',
    'test_fury_current_cat2_short_horizon_sensitivity_v1.py',
    'test_fury_expert_guided_search_v1.py',
    'test_fury_shadow_sim_seedability_v1.py',
    'test_fury_timer_source_recovery_summary.py',
    'test_fury_timer_transition_contract_v1.py',
    'test_o2o_runtime_contract.py',
    'test_shadow_pair_export_addon_contract.py',
    'test_simulator_binary_contract_v1.py',
    'test_timer_calibration_campaign_contract.py',
    'test_timer_calibration_debug_contract.py',
    'test_white_rage_phase11_external_holdout_audit.py'
)

$modules = @(
    Get-ChildItem -LiteralPath (Join-Path $projectRoot 'tests') -Filter 'test_*.py' -File |
        Where-Object { $_.Name -notin $excluded } |
        Sort-Object Name |
        ForEach-Object { 'tests.' + $_.BaseName }
)
if ($modules.Count -eq 0) {
    throw 'No source-only Python test modules were selected.'
}

Push-Location $projectRoot
try {
    py -3 -B -m unittest @modules
    if ($LASTEXITCODE -ne 0) {
        throw 'Source-only Python tests failed.'
    }
}
finally {
    Pop-Location
}

Write-Output "Source-only suite passed: $($modules.Count) modules."

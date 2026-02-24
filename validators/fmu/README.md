# FMU Validator Cloud Run Job

Minimal Cloud Run Job container for FMU validators. It:

1. Downloads `input.json` from GCS (typed `FMUInputEnvelope`)
2. Downloads the FMU from `input_files[role="fmu"]`
3. Runs a short `fmpy.simulate_fmu` window with provided inputs
4. Writes `output.json` back to GCS and POSTs the callback

The container mirrors the contract defined in `validibot_shared.fmu.envelopes`.
Use the Django launcher to provide catalog-keyed inputs and callback info.

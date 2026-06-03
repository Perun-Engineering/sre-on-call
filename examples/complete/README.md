# Complete example — sre-on-call

A runnable reference root that consumes the [`sre-on-call`](../../modules/sre-on-call)
module. It owns the `provider`/`backend` and builds the Lambda artifacts from the
repo root, so the module stays consumable from any root (including Terragrunt in
an existing environment).

## Usage

```bash
cd examples/complete
cp terraform.tfvars.example terraform.tfvars   # fill in agent_container_registry
terraform init
terraform plan
terraform apply
```

`agent_container_registry` is the only required input; everything else defaults.
The Lambda build needs the repo's `.venv` (`uv sync`) and `lambda_adapter/`,
`shared/`, `pyproject.toml` at the repo root — already wired via `source_root`.

## Migrating from the old flat `terraform/` root

Earlier deployments ran from a flat `terraform/` root with **local state**. To
adopt the module layout without destroying anything:

1. Relocate the local state next to this root (state is gitignored, not in the repo):
   ```bash
   mv ../../terraform/terraform.tfstate* .
   ```
   (Skip if you use a remote backend — just point this root at the same backend.)
2. `terraform init`
3. `terraform plan` — the [`moved.tf`](./moved.tf) blocks re-key every resource
   under `module.sre_on_call.*`. **Expected result: no changes.** If the plan
   shows any destroy/create, stop and reconcile before applying.
4. Once a clean apply confirms the move, `moved.tf` is inert and may be deleted.

## Consuming from Terragrunt instead

Point a `terragrunt.hcl` at `../../modules/sre-on-call` and pass the same inputs.
Set `source_root`/`config_path` to wherever the app source and `config.yaml`
live in your checkout. Provider and backend come from your Terragrunt
`generate`/`remote_state` blocks.

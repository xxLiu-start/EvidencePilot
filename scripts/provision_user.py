"""Provision an OIDC user into a tenant and project without trusting token role claims.

Example:
  python -m scripts.provision_user --tenant acme --tenant-name "Acme Research" \
      --issuer https://id.example/realms/acme --subject 9f... \
      --email researcher@acme.example --tenant-role RESEARCHER \
      --project default --project-role RESEARCHER
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db.models import Project, ProjectMembership, Tenant, TenantMembership, User
from app.db.session import SessionLocal


TENANT_ROLES = {"TENANT_ADMIN", "RESEARCHER", "VIEWER"}
PROJECT_ROLES = {"PROJECT_MANAGER", "RESEARCHER", "REVIEWER", "VIEWER"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision an OIDC identity for EvidencePilot")
    parser.add_argument("--tenant", required=True, help="Tenant slug")
    parser.add_argument("--tenant-name", required=True, help="Tenant display name")
    parser.add_argument("--external-tenant-id", help="Optional tenant_id claim value issued by the IdP")
    parser.add_argument("--issuer", required=True, help="Exact OIDC issuer URL")
    parser.add_argument("--subject", required=True, help="Exact OIDC sub claim")
    parser.add_argument("--email")
    parser.add_argument("--tenant-role", default="RESEARCHER", choices=sorted(TENANT_ROLES))
    parser.add_argument("--project", default="default", help="Project slug")
    parser.add_argument("--project-role", default="RESEARCHER", choices=sorted(PROJECT_ROLES))
    args = parser.parse_args()

    if not args.issuer.startswith("https://") and not args.issuer.startswith("http://localhost"):
        parser.error("--issuer must be HTTPS (HTTP is allowed only for localhost development)")

    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == args.tenant))
        if tenant is None:
            tenant = Tenant(slug=args.tenant, name=args.tenant_name,
                            external_id=args.external_tenant_id, is_active=True)
            db.add(tenant)
            db.flush()
        elif args.external_tenant_id and tenant.external_id not in {None, args.external_tenant_id}:
            parser.error("Tenant slug already maps to a different external tenant ID")
        elif args.external_tenant_id and tenant.external_id is None:
            tenant.external_id = args.external_tenant_id

        project = db.scalar(select(Project).where(
            Project.tenant_id == tenant.id, Project.slug == args.project
        ))
        if project is None:
            project = Project(tenant_id=tenant.id, slug=args.project,
                              name=args.project.replace("-", " ").title(), is_active=True)
            db.add(project)
            db.flush()

        user = db.scalar(select(User).where(User.issuer == args.issuer, User.subject == args.subject))
        if user is None:
            user = User(issuer=args.issuer, subject=args.subject, email=args.email, is_active=True)
            db.add(user)
            db.flush()
        else:
            user.is_active = True
            if args.email:
                user.email = args.email

        tenant_membership = db.scalar(select(TenantMembership).where(
            TenantMembership.tenant_id == tenant.id, TenantMembership.user_id == user.id
        ))
        if tenant_membership is None:
            db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id,
                                    role=args.tenant_role, is_active=True))
        else:
            tenant_membership.role = args.tenant_role
            tenant_membership.is_active = True

        project_membership = db.scalar(select(ProjectMembership).where(
            ProjectMembership.project_id == project.id, ProjectMembership.user_id == user.id
        ))
        if project_membership is None:
            db.add(ProjectMembership(project_id=project.id, user_id=user.id,
                                     role=args.project_role, is_active=True))
        else:
            project_membership.role = args.project_role
            project_membership.is_active = True

        db.commit()
        print(f"Provisioned user_id={user.id} tenant_id={tenant.id} project_id={project.id}")


if __name__ == "__main__":
    main()

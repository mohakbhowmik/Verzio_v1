from database import SessionLocal, Incident


def create_incident(
    severity: str,
    module: str,
    title: str,
    message: str,
    business_id=None,
    phone_number=None,
    stack_trace=None,
):
    db = SessionLocal()

    try:
        incident = Incident(
            severity=severity,
            module=module,
            title=title,
            message=message,
            business_id=business_id,
            phone_number=phone_number,
            stack_trace=stack_trace,
        )

        db.add(incident)
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()
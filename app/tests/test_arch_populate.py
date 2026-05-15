import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal, Project, Task, FileSummary, ArchGenJob
import json
import os
from sqlalchemy import or_

client = TestClient(app)

def cleanup_project(db, project_name):
    p = db.query(Project).filter(Project.name == project_name).first()
    if p:
        db.query(ArchGenJob).filter(ArchGenJob.project_id == p.id).delete()
        db.query(Task).filter(or_(Task.project == project_name, Task.project_id == p.id)).delete()
        
        # We don't have project_id on FileSummary, so we cleanup by path prefix
        # We need to be careful here to match what's in the DB.
        root = os.path.normpath(os.path.abspath(p.path))
        prefix = root.replace("\\", "/") + "/"
        prefix_back = root + "\\"
        db.query(FileSummary).filter(
            or_(
                FileSummary.file_path.like(prefix + "%"),
                FileSummary.file_path.like(prefix_back + "%")
            )
        ).delete(synchronize_session=False)
        
        db.query(Project).filter(Project.id == p.id).delete()
        db.commit()

@pytest.fixture
def project_setup():
    db = SessionLocal()
    cleanup_project(db, "TestProject")

    # Create project
    test_path = os.path.abspath("/tmp/testproject")
    p = Project(name="TestProject", path=test_path, description="Test Description")
    db.add(p)
    db.commit()
    db.refresh(p)
    
    yield p
    
    # Cleanup after tests
    cleanup_project(db, "TestProject")
    db.close()

def test_preview_populate_arch_empty(project_setup):
    response = client.get("/api/projects/TestProject/populate-arch/preview")
    assert response.status_code == 200
    data = response.json()
    assert "categories_to_generate" in data
    assert len(data["categories_to_generate"]) > 0
    assert data["has_file_summaries"] is False
    assert data["file_summary_count"] == 0

def test_preview_populate_arch_with_tasks(project_setup):
    db = SessionLocal()
    try:
        # Add an architecture task
        t = Task(
            id="arch-1", 
            project="TestProject", 
            project_id=project_setup.id,
            type="architecture", 
            title="Arch Card", 
            content=json.dumps({"category": "Platform", "priority": "normal"})
        )
        db.add(t)
        db.commit()
        
        response = client.get("/api/projects/TestProject/populate-arch/preview")
        assert response.status_code == 200
        data = response.json()
        assert "Platform" not in data["categories_to_generate"]
        assert "Design" in data["categories_to_generate"]
    finally:
        db.close()

def test_preview_populate_arch_with_summaries(project_setup):
    db = SessionLocal()
    try:
        # Add file summaries
        test_path = os.path.abspath("/tmp/testproject")
        s1 = FileSummary(
            sha1_hash="abc", 
            file_size_bytes=100, 
            file_path=os.path.join(test_path, "main.py"), 
            summary="main"
        )
        s2 = FileSummary(
            sha1_hash="def", 
            file_size_bytes=200, 
            file_path=os.path.join(test_path, "utils.py"), 
            summary="utils"
        )
        db.add(s1)
        db.add(s2)
        db.commit()
        
        response = client.get("/api/projects/TestProject/populate-arch/preview")
        assert response.status_code == 200
        data = response.json()
        assert data["has_file_summaries"] is True
        assert data["file_summary_count"] == 2
    finally:
        db.close()

def test_preview_populate_arch_with_active_jobs(project_setup):
    db = SessionLocal()
    try:
        # Add an active job
        job = ArchGenJob(project_id=project_setup.id, category="Security", status="pending")
        db.add(job)
        db.commit()
        
        response = client.get("/api/projects/TestProject/populate-arch/preview")
        assert response.status_code == 200
        data = response.json()
        assert "Security" not in data["categories_to_generate"]
        assert "Security" in data["active_jobs"]
    finally:
        db.close()

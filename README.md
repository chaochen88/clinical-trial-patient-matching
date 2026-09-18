# Clinical-trial-patient-matching

A robust, reproducible development environment built inside **Windows Subsystem for Linux (WSL)** utilizing **Pixi** for cross-platform package management and **Docker** for containerization.

## 🛠 Prerequisites

Ensure you have the following installed on your host system:
*   [Windows Subsystem for Linux (WSL 2)](https://microsoft.com) - Running **Ubuntu**.
*   [Docker Desktop](https://docker.com) - With WSL 2 backend integration enabled.
*   [Pixi](https://pixi.sh) - Installed natively inside Ubuntu.

## 🚀 Getting Started

### 1. Project Initialization
Initialize your development environment using Pixi:
```bash
pixi init
```

### 2. Adding Dependencies
Add required Python or system packages directly from the `conda-forge` registry:
```bash
pixi add python numpy pandas
```

### 3. Running the Application
Execute your scripts in an isolated, locked environment context:
```bash
pixi run python main.py
```

## 🐋 Docker Integration

Build and run your containerized pipeline seamlessly using the integrated backend:
```bash
# Build the Docker image
docker build -t my-app .

# Run the container
docker run -it --rm my-app
```


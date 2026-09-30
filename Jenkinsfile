pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '15'))
    }

    environment {
        IMAGE_NAME = "sha-mapping-engine"
        IMAGE_TAG = "${BUILD_NUMBER}"

        CONTAINER_NAME = "sha-mapping-engine"

        HOST_PORT = "8099"
        CONTAINER_PORT = "8099"
    }

    stages {

        stage('Checkout Code') {
            steps {
                checkout scm
            }
        }

        stage('Verify Syntax') {
            steps {
                sh '''
                    echo "Checking Python files syntax..."
                    python3 -m py_compile app.py common/config.py common/db.py || true
                '''
            }
        }

        stage('Build Docker Image') {
            steps {
                sh '''
                    echo "Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG} and ${IMAGE_NAME}:latest"
                    docker build \
                        -t ${IMAGE_NAME}:${IMAGE_TAG} \
                        -t ${IMAGE_NAME}:latest \
                        -f Dockerfile \
                        .
                '''
            }
        }

        stage('Remove Existing Container') {
            steps {
                sh '''
                    echo "Removing existing container ${CONTAINER_NAME} if present..."
                    docker rm -f ${CONTAINER_NAME} || true
                '''
            }
        }

        stage('Deploy Docker Container') {
            steps {
                sh '''
                    echo "Starting new container ${CONTAINER_NAME} on port ${HOST_PORT}..."
                    mkdir -p data cache logs

                    # Run container with persistent host volumes and environment variables
                    if [ -f .env ]; then
                        ENV_ARG="--env-file .env"
                    else
                        echo "Warning: .env file not found in workspace, starting without --env-file"
                        ENV_ARG=""
                    fi

                    docker run -d \
                        --name ${CONTAINER_NAME} \
                        --restart unless-stopped \
                        ${ENV_ARG} \
                        -v "$(pwd)/data:/app/data" \
                        -v "$(pwd)/cache:/app/cache" \
                        -v "$(pwd)/logs:/app/logs" \
                        -p ${HOST_PORT}:${CONTAINER_PORT} \
                        ${IMAGE_NAME}:${IMAGE_TAG}
                '''
            }
        }

        stage('Health Check') {
            steps {
                sh '''
                    echo "Validating service liveness on port ${HOST_PORT}..."
                    for i in $(seq 1 12); do
                        if curl -sf "http://localhost:${HOST_PORT}/health" > /dev/null; then
                            echo "Container is healthy and responding!"
                            exit 0
                        fi
                        echo "Waiting for service to become ready ($i/12)..."
                        sleep 5
                    done

                    echo "Healthcheck failed! Dumping container logs:"
                    docker logs --tail 50 ${CONTAINER_NAME}
                    exit 1
                '''
            }
        }

        stage('Cleanup Old Images') {
            steps {
                sh '''
                    echo "Cleaning up dangling and older images..."
                    docker images ${IMAGE_NAME} --format "{{.Repository}}:{{.Tag}}" \
                        | grep -v "^${IMAGE_NAME}:${IMAGE_TAG}$" \
                        | grep -v "^${IMAGE_NAME}:latest$" \
                        | xargs -r docker rmi || true
                '''
            }
        }
    }

    post {
        success {
            echo "Deployment completed successfully! Service is live at http://localhost:${HOST_PORT}"
        }

        failure {
            echo "Deployment failed! Please check the stage logs above."
        }
    }
}

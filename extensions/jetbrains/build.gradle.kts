plugins {
    id("java")
    id("org.jetbrains.intellij") version "1.15.0"
}

group = "com.maxwell"
version = "1.0-SNAPSHOT"

repositories {
    mavenCentral()
}

dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter-api:5.8.1")
    testRuntimeOnly("org.junit.jupiter:junit-jupiter-engine:5.8.1")
}

intellij {
    version.set("2023.1.5")
    type.set("IC")
}

tasks.getByName<Test>("test") {
    useJUnitPlatform()
}
